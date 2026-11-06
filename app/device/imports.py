"""Batch CSV import by stakeholders (cluster ADMIN: own-cluster devices;
SUPER_ADMIN: all). USER/GUEST: 403.

Format = the public API export (GET /api/v1/export?format=csv) for the same
dataset: same column names, raw values, ISO-8601 UTC timestamps. The
informational columns (id, device_name, region, community, cluster_id) may be
present and are ignored — the device is identified by device_uuid alone, and
ids are assigned fresh.

The body is the raw CSV (Content-Type: text/csv), not multipart: FastAPI's
multipart support needs python-multipart, which this project doesn't ship.

Imported rows are real data (is_test=False) tagged with import_batch_id. An
import never fires notifications and never touches device liveness
(last_activity / last_sensor_data_at / offline_since) or position — those
describe the device's live link, not historical data. It does add imported
detections to total_mosquito_count, exactly like ingest.
"""
import csv
import io
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import case, func, text, tuple_, update
from sqlalchemy.orm import Session

from app.api_access.queries import MOSQUITO_EVENT_COLUMNS, SENSOR_READING_COLUMNS
from app.audit.models import AuditAction
from app.audit.recorder import audit
from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.core.security.permissions import is_super_admin, require_admin
from app.device.models import (
    LIVE_MOSQUITO_EVENT, LIVE_SENSOR_READING, DataImport, Device, MosquitoEvent,
    MosquitoIndividualReading, SensorDeviceReading,
)

router = APIRouter()

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 50_000
MAX_REPORTED = 500  # errors / skipped / conflict row numbers listed in a report
# Clock skew allowed between the importer's data and the server before a
# timestamp counts as "in the future".
FUTURE_TOLERANCE = timedelta(minutes=5)
# pg_advisory_xact_lock key serialising import writes (any constant unique
# to this feature).
IMPORT_LOCK_KEY = 0x1D_A7A1
_CHUNK = 500  # bound parameter count per IN query

SENSOR = "sensor-readings"
MOSQUITO = "mosquito-events"

# Export columns that describe the device or the row's old id, not the reading.
IGNORED_COLUMNS = {"id", "device_name", "region", "community", "cluster_id"}
SENSOR_FLOATS = [
    "external_temperature", "internal_temperature", "external_humidity", "internal_humidity",
    "external_pressure", "internal_pressure", "external_light", "battery_voltage",
]
# String columns and their DB length limits.
MOSQUITO_STRINGS = {"species": 250, "genus": 250, "age_group": 50, "sex": 50}

EXPORT_COLUMNS = {SENSOR: list(SENSOR_READING_COLUMNS), MOSQUITO: list(MOSQUITO_EVENT_COLUMNS)}
REQUIRED_COLUMNS = {k: [c for c in v if c not in IGNORED_COLUMNS] for k, v in EXPORT_COLUMNS.items()}


class RowError(Exception):
    pass


# ── Cell parsing (inverse of public_routes._csv_escape / serialize_value) ────

def _cell(value: str) -> str:
    """Undo the export's formula-injection guard ("'=..." → "="). Text is
    otherwise kept byte-for-byte (no strip) so it round-trips exactly."""
    if value == "'":
        # Exports made before the _csv_escape fix wrote every "" as a lone "'".
        return ""
    if value[:1] == "'" and value[1:2] in ("=", "+", "-", "@"):
        return value[1:]
    return value


def parse_timestamp(value: str, column: str) -> datetime:
    """ISO 8601 with a time AND an explicit zone, as the export writes it
    ("2026-05-01T12:00:00.123456Z"). Offsets are converted to naive UTC."""
    value = value.strip()
    example = "e.g. 2026-05-01T12:00:00Z"
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        raise RowError(f"{column}: '{value}' is not an ISO 8601 timestamp ({example})")
    if len(value) <= 10:
        raise RowError(f"{column}: '{value}' has no time of day ({example})")
    if ts.tzinfo is None:
        raise RowError(f"{column}: '{value}' has no timezone — add Z for UTC or an offset like +02:00 ({example})")
    ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    if ts > datetime.utcnow() + FUTURE_TOLERANCE:
        raise RowError(f"{column}: '{value}' is in the future")
    return ts


def parse_float(value: str, column: str) -> Optional[float]:
    value = value.strip()
    if value == "":
        return None  # empty is "not measured" — never 0
    try:
        number = float(value)
    except ValueError:
        raise RowError(f"{column}: '{value}' is not a number")
    if not math.isfinite(number):
        raise RowError(f"{column}: '{value}' is not a finite number")
    return number


def parse_bool(value: str, column: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("true", "on", "1"):
        return True
    if lowered in ("false", "off", "0"):
        return False
    raise RowError(f"{column}: '{value}' must be True/False (or ON/OFF)")


def parse_sensor_row(cells: dict) -> dict:
    row = {"timestamp": parse_timestamp(cells["timestamp"], "timestamp")}
    for column in SENSOR_FLOATS:
        row[column] = parse_float(cells[column], column)
    row["trap_status"] = parse_bool(cells["trap_status"], "trap_status")
    return row


def parse_mosquito_row(cells: dict) -> dict:
    row = {"timestamp": parse_timestamp(cells["timestamp"], "timestamp")}
    # Ingest writes exactly one event row per detection, always count=1.
    if cells["count"].strip() != "1":
        raise RowError(f"count: '{cells['count']}' must be 1 (one row per detection, as devices send)")
    row["count"] = 1
    for column, max_len in MOSQUITO_STRINGS.items():
        if len(cells[column]) > max_len:
            raise RowError(f"{column}: longer than {max_len} characters")
    if cells["detection_timestamp"] == "":
        # The export's outer join writes an event without a detection as
        # all-empty detection columns; anything else is a broken row.
        if any(cells[c] for c in MOSQUITO_STRINGS):
            raise RowError("detection_timestamp is required when species/genus/age_group/sex are given")
        row["detection"] = None
    else:
        row["detection"] = {
            "detection_timestamp": parse_timestamp(cells["detection_timestamp"], "detection_timestamp"),
            # Same as ingest: species/genus stored as given (empty → NULL);
            # age_group/sex are NOT NULL and degrade to "".
            "species": cells["species"] or None,
            "genus": cells["genus"] or None,
            "age_group": cells["age_group"],
            "sex": cells["sex"],
        }
    return row


# ── Duplicate keys (compared in export form: NULL and "" both export as "") ──

def _sensor_key(device_id: int, r: dict) -> tuple:
    return (device_id, r["timestamp"], *(r[c] for c in SENSOR_FLOATS), bool(r["trap_status"]))


def _mosquito_key(device_id, timestamp, count, detection_ts, species, genus, age_group, sex) -> tuple:
    return (device_id, timestamp, count, detection_ts,
            species or "", genus or "", age_group or "", sex or "")


def _existing_keys(session: Session, kind: str, pairs: list[tuple[int, datetime]]) -> Counter:
    """Multiset of live rows already stored at the given (device_id, timestamp) pairs."""
    keys: Counter = Counter()
    for i in range(0, len(pairs), _CHUNK):
        chunk = pairs[i:i + _CHUNK]
        if kind == SENSOR:
            rows = (
                session.query(SensorDeviceReading)
                .filter(LIVE_SENSOR_READING,
                        tuple_(SensorDeviceReading.device_id, SensorDeviceReading.timestamp).in_(chunk))
                .all()
            )
            keys.update(_sensor_key(r.device_id, {c: getattr(r, c) for c in ("timestamp", *SENSOR_FLOATS, "trap_status")})
                        for r in rows)
        else:
            rows = (
                session.query(
                    MosquitoEvent.device_id, MosquitoEvent.timestamp, MosquitoEvent.count,
                    MosquitoIndividualReading.detection_timestamp, MosquitoIndividualReading.species,
                    MosquitoIndividualReading.genus, MosquitoIndividualReading.age_group,
                    MosquitoIndividualReading.sex,
                )
                .outerjoin(MosquitoIndividualReading, MosquitoIndividualReading.batch_id == MosquitoEvent.id)
                .filter(LIVE_MOSQUITO_EVENT,
                        tuple_(MosquitoEvent.device_id, MosquitoEvent.timestamp).in_(chunk))
                .all()
            )
            keys.update(_mosquito_key(*r) for r in rows)
    return keys


def _row_key(kind: str, device_id: int, r: dict) -> tuple:
    if kind == SENSOR:
        return _sensor_key(device_id, r)
    d = r["detection"] or {}
    return _mosquito_key(device_id, r["timestamp"], r["count"], d.get("detection_timestamp"),
                         d.get("species"), d.get("genus"), d.get("age_group"), d.get("sex"))


# ── Validation ───────────────────────────────────────────────────────────────

def _decode(body: bytes) -> str:
    if len(body) > MAX_FILE_BYTES:
        raise HTTPException(413, f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    try:
        text = body.decode("utf-8-sig")  # tolerate Excel's BOM
    except UnicodeDecodeError:
        raise HTTPException(422, "File is not UTF-8 text")
    return text


def validate(session: Session, user: UserResponse, kind: str, body: bytes) -> tuple[dict, list]:
    """→ (report, rows to insert). Rows are empty whenever the report has errors."""
    reader = csv.DictReader(io.StringIO(_decode(body)))
    try:
        header_clean = [h.strip() for h in reader.fieldnames or []]
    except csv.Error as exc:
        raise HTTPException(422, f"CSV could not be parsed (line {reader.line_num}): {exc}")
    reader.fieldnames = header_clean
    unknown = sorted(set(header_clean) - set(EXPORT_COLUMNS[kind]))
    missing = [c for c in REQUIRED_COLUMNS[kind] if c not in header_clean]
    if unknown or missing or len(set(header_clean)) != len(header_clean):
        raise HTTPException(422, {
            "message": "CSV header does not match the export format",
            "unknown_columns": unknown, "missing_columns": missing,
            "expected_columns": EXPORT_COLUMNS[kind],
        })

    raw_rows = []
    try:
        for raw in reader:
            raw_rows.append(raw)
            if len(raw_rows) > MAX_ROWS:
                raise HTTPException(413, f"File has more than {MAX_ROWS} data rows")
    except csv.Error as exc:  # e.g. a field over csv.field_size_limit()
        raise HTTPException(422, f"CSV could not be parsed (line {reader.line_num}): {exc}")
    if not raw_rows:
        raise HTTPException(422, "File has no data rows")

    devices: dict[str, Device] = {}
    uuid_list = sorted({(r.get("device_uuid") or "").strip() for r in raw_rows})
    for i in range(0, len(uuid_list), _CHUNK):
        for d in session.query(Device).filter(Device.device_uuid.in_(uuid_list[i:i + _CHUNK])):
            devices[d.device_uuid] = d

    parse = parse_sensor_row if kind == SENSOR else parse_mosquito_row
    errors, parsed = [], []
    for row_number, raw in enumerate(raw_rows, start=2):  # spreadsheet line; header is 1
        try:
            if None in raw or any(v is None for v in raw.values()):
                raise RowError("wrong number of columns")
            nul = next((k for k, v in raw.items() if "\x00" in v), None)
            if nul:
                raise RowError(f"{nul}: contains a NUL (\\x00) character")
            cells = {k: _cell(v) for k, v in raw.items()}
            cells["device_uuid"] = cells["device_uuid"].strip()
            device = devices.get(cells["device_uuid"])
            # One message for unknown and out-of-scope: don't reveal which.
            # An admin with no cluster owns nothing (never "NULL == NULL").
            if device is None or not (is_super_admin(user) or
                                      (user.cluster_id is not None and device.cluster_id == user.cluster_id)):
                raise RowError(f"device_uuid: '{cells['device_uuid']}' is not a device in your cluster")
            parsed.append((row_number, device, parse(cells)))
        except RowError as exc:
            errors.append({"row": row_number, "error": str(exc)})

    report = {
        "kind": kind, "rows_total": len(raw_rows), "error_count": len(errors),
        "errors": errors[:MAX_REPORTED], "rows_to_import": 0, "rows_skipped": 0,
        "skipped_rows": [], "conflict_count": 0, "conflict_rows": [],
        "expected_columns": EXPORT_COLUMNS[kind],
    }
    if errors:
        return report, []

    existing = _existing_keys(session, kind, sorted({(d.id, r["timestamp"]) for _, d, r in parsed}))
    occupied = {key[:2] for key in existing}  # (device_id, timestamp) already stored
    to_insert, skipped, conflicts = [], [], []
    for row_number, device, r in parsed:
        key = _row_key(kind, device.id, r)
        if existing[key] > 0:  # multiset: one stored row absorbs one file row
            existing[key] -= 1
            skipped.append(row_number)
        else:
            to_insert.append((device, r))
            # Imported anyway, but flagged: same device + time, different values.
            if key[:2] in occupied:
                conflicts.append(row_number)
    return report | {
        "rows_to_import": len(to_insert), "rows_skipped": len(skipped),
        "skipped_rows": skipped[:MAX_REPORTED],
        "conflict_count": len(conflicts), "conflict_rows": conflicts[:MAX_REPORTED],
    }, to_insert


# ── Write ────────────────────────────────────────────────────────────────────

def _write(session: Session, user: UserResponse, kind: str, filename: Optional[str],
           report: dict, rows: list) -> DataImport:
    clusters = {d.cluster_id for d, _ in rows}
    batch = DataImport(
        kind=kind, filename=(filename or None) and filename[:255],
        cluster_id=clusters.pop() if len(clusters) == 1 else None,
        rows_total=report["rows_total"], rows_imported=len(rows),
        rows_skipped=report["rows_skipped"], imported_by_user_id=user.id,
    )
    session.add(batch)
    session.flush()

    detections: Counter = Counter()
    for device, r in rows:
        if kind == SENSOR:
            session.add(SensorDeviceReading(
                device_id=device.id, import_batch_id=batch.id, is_test=False,
                timestamp=r["timestamp"], trap_status=r["trap_status"],
                **{c: r[c] for c in SENSOR_FLOATS},
            ))
        else:
            d = r["detection"]
            session.add(MosquitoEvent(
                device_id=device.id, import_batch_id=batch.id, is_test=False,
                timestamp=r["timestamp"], count=r["count"],
                mosquito_reading=MosquitoIndividualReading(**d) if d else None,
            ))
            # Same unit as ingest (+1 per detection) and as the single-event
            # delete, which subtracts the reading rows it removes.
            if d:
                detections[device] += 1
    for device, n in detections.items():
        _add_to_count(session, device.id, n)
    session.commit()
    session.refresh(batch)
    return batch


def _add_to_count(session: Session, device_id: int, n: int) -> None:
    """Atomic SQL increment (a negative n decrements, floored at 0) — never a
    read-modify-write that could lose a concurrent ingest's +1."""
    new = func.coalesce(Device.total_mosquito_count, 0) + n
    if n < 0:
        new = case((new < 0, 0), else_=new)
    session.execute(
        update(Device).where(Device.id == device_id)
        .values(total_mosquito_count=new).execution_options(synchronize_session=False)
    )


def _run_import(kind: str, body: bytes, dry_run: bool, filename: Optional[str],
                session: Session, user: UserResponse) -> dict:
    if not dry_run and session.get_bind().dialect.name == "postgresql":
        # Serialise real imports: held from the duplicate check through the
        # insert, released by the commit/rollback, so two concurrent imports
        # of the same file can't both pass the check and both insert.
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IMPORT_LOCK_KEY})
    report, rows = validate(session, user, kind, body)
    if dry_run:
        return report | {"dry_run": True, "imported": False}
    if report["error_count"]:
        # All-or-nothing: one bad row and nothing is written.
        raise HTTPException(422, report | {"message": f"{report['error_count']} row(s) failed validation; nothing was imported"})
    batch = _write(session, user, kind, filename, report, rows)
    audit(session, AuditAction.DATA_IMPORTED, actor_user_id=user.id, actor_email=user.email,
          target_type="data_import", target_id=batch.id,
          detail={"kind": kind, "filename": batch.filename, "rows_imported": batch.rows_imported,
                  "rows_skipped": batch.rows_skipped})
    return report | {"dry_run": False, "imported": True, "batch_id": batch.id}


_CSV_BODY = Body(..., media_type="text/csv", description="The CSV file contents (same columns as /api/v1/export)")


@router.post("/sensor-readings")
def import_sensor_readings(
    body: bytes = _CSV_BODY,
    dry_run: bool = Query(default=False, description="Validate and report only; write nothing"),
    filename: Optional[str] = Query(default=None, max_length=255),
    session: Session = Depends(get_db),
    user: UserResponse = Depends(require_admin),
):
    return _run_import(SENSOR, body, dry_run, filename, session, user)


@router.post("/mosquito-events")
def import_mosquito_events(
    body: bytes = _CSV_BODY,
    dry_run: bool = Query(default=False, description="Validate and report only; write nothing"),
    filename: Optional[str] = Query(default=None, max_length=255),
    session: Session = Depends(get_db),
    user: UserResponse = Depends(require_admin),
):
    return _run_import(MOSQUITO, body, dry_run, filename, session, user)


# ── Batches ──────────────────────────────────────────────────────────────────

class DataImportResponse(BaseModel):
    id: int
    kind: str
    filename: Optional[str] = None
    cluster_id: Optional[int] = None
    rows_total: int
    rows_imported: int
    rows_skipped: int
    imported_by_user_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


def _scoped(session: Session, user: UserResponse):
    q = session.query(DataImport)
    if not is_super_admin(user):
        # Mixed-cluster (NULL) batches are super-admin only; so is everything
        # for an admin without a cluster (a NULL filter would match those).
        if user.cluster_id is None:
            return q.filter(False)
        q = q.filter(DataImport.cluster_id == user.cluster_id)
    return q


@router.get("", response_model=list[DataImportResponse])
def list_imports(session: Session = Depends(get_db), user: UserResponse = Depends(require_admin)):
    return _scoped(session, user).order_by(DataImport.created_at.desc(), DataImport.id.desc()).all()


@router.delete("/{import_id}", status_code=204)
def delete_import(import_id: int, session: Session = Depends(get_db),
                  user: UserResponse = Depends(require_admin)):
    batch = _scoped(session, user).filter(DataImport.id == import_id).first()
    if batch is None:
        raise HTTPException(404, "Import not found")

    # Reverse total_mosquito_count by the detections this batch added.
    per_device = (
        session.query(MosquitoEvent.device_id, MosquitoIndividualReading.id)
        .join(MosquitoIndividualReading, MosquitoIndividualReading.batch_id == MosquitoEvent.id)
        .filter(MosquitoEvent.import_batch_id == batch.id)
        .all()
    )
    detections = Counter(device_id for device_id, _ in per_device)
    for device_id, n in detections.items():
        _add_to_count(session, device_id, -n)

    event_ids = session.query(MosquitoEvent.id).filter(MosquitoEvent.import_batch_id == batch.id)
    session.query(MosquitoIndividualReading).filter(
        MosquitoIndividualReading.batch_id.in_(event_ids.scalar_subquery())
    ).delete(synchronize_session=False)
    events = session.query(MosquitoEvent).filter(MosquitoEvent.import_batch_id == batch.id).delete(synchronize_session=False)
    readings = session.query(SensorDeviceReading).filter(
        SensorDeviceReading.import_batch_id == batch.id).delete(synchronize_session=False)
    detail = {"kind": batch.kind, "filename": batch.filename,
              "rows_deleted": events + readings, "mosquito_count_reversed": sum(detections.values())}
    session.delete(batch)
    session.commit()
    audit(session, AuditAction.DATA_IMPORT_DELETED, actor_user_id=user.id, actor_email=user.email,
          target_type="data_import", target_id=import_id, detail=detail)
