"""Query builders + serialization for the public data API.

Deliberately NOT reusing the app's service layer: several of those methods load
the full match into memory and slice with paginate(), which is fine for a
20-row dashboard page and fatal for a researcher walking millions of rows.
Everything here is column-tuple selects (no ORM instantiation), SQL-side
keyset pagination, and streaming-friendly statements.
"""
import base64
from datetime import datetime
from typing import Optional, Sequence

from fastapi import HTTPException
from sqlalchemy import Select, select, tuple_
from sqlalchemy.orm import Session

from app.authentication.schema import UserResponse
from app.core.security.permissions import visible_cluster_ids
from app.device.models import (
    Device,
    DeviceCluster,
    MosquitoEvent,
    MosquitoIndividualReading,
    SensorDeviceReading,
)

# ── Datasets ─────────────────────────────────────────────────────────────────
# Ordered name → column expression. The order here IS the documented CSV
# column order — never reorder without a docs update.

SENSOR_READING_COLUMNS = {
    "id": SensorDeviceReading.id,
    "timestamp": SensorDeviceReading.timestamp,
    "device_uuid": Device.device_uuid,
    "device_name": Device.name,
    "region": Device.region,
    "community": Device.community,
    "cluster_id": Device.cluster_id,
    "external_temperature": SensorDeviceReading.external_temperature,
    "internal_temperature": SensorDeviceReading.internal_temperature,
    "external_humidity": SensorDeviceReading.external_humidity,
    "internal_humidity": SensorDeviceReading.internal_humidity,
    "external_pressure": SensorDeviceReading.external_pressure,
    "internal_pressure": SensorDeviceReading.internal_pressure,
    "external_light": SensorDeviceReading.external_light,
    "battery_voltage": SensorDeviceReading.battery_voltage,
    "trap_status": SensorDeviceReading.trap_status,
}

MOSQUITO_EVENT_COLUMNS = {
    "id": MosquitoEvent.id,
    "timestamp": MosquitoEvent.timestamp,
    "device_uuid": Device.device_uuid,
    "device_name": Device.name,
    "region": Device.region,
    "community": Device.community,
    "cluster_id": Device.cluster_id,
    "count": MosquitoEvent.count,
    "detection_timestamp": MosquitoIndividualReading.detection_timestamp,
    "species": MosquitoIndividualReading.species,
    "genus": MosquitoIndividualReading.genus,
    "age_group": MosquitoIndividualReading.age_group,
    "sex": MosquitoIndividualReading.sex,
}

DEVICE_COLUMNS = {
    "id": Device.id,
    "device_uuid": Device.device_uuid,
    "name": Device.name,
    "description": Device.description,
    "region": Device.region,
    "community": Device.community,
    "longitude": Device.longitude,
    "latitude": Device.latitude,
    "cluster_id": Device.cluster_id,
    "total_mosquito_count": Device.total_mosquito_count,
    "last_activity": Device.last_activity,
    "created_at": Device.created_at,
}

CLUSTER_COLUMNS = {
    "id": DeviceCluster.id,
    "cluster_uuid": DeviceCluster.cluster_uuid,
    "name": DeviceCluster.name,
    "description": DeviceCluster.description,
    "public": DeviceCluster.public,
    "created_at": DeviceCluster.created_at,
}

DEVICE_SORT_COLUMNS = {
    "created_at": Device.created_at,
    "name": Device.name,
    "last_activity": Device.last_activity,
    "total_mosquito_count": Device.total_mosquito_count,
}


# ── Scope ────────────────────────────────────────────────────────────────────

def resolve_scope(
    session: Session, user: UserResponse, requested_cluster_ids: Optional[Sequence[int]]
) -> Optional[set[int]]:
    """Intersect an optional requested cluster filter with the caller's scope.

    Returns None for "unrestricted" (super admin, no filter). A scoped caller
    asking for clusters outside their scope gets the INTERSECTION — an entirely
    out-of-scope request therefore yields the empty set, i.e. zero rows, and
    never falls back to their full visible scope.
    """
    visible = visible_cluster_ids(session, user)
    if requested_cluster_ids:
        requested = set(requested_cluster_ids)
        return requested if visible is None else (visible & requested)
    return visible


def apply_scope(stmt: Select, allowed: Optional[set[int]]) -> Select:
    if allowed is not None:
        # Empty set compiles to WHERE false — zero rows, exactly as intended.
        stmt = stmt.where(Device.cluster_id.in_(allowed))
    return stmt


# ── Field selection ──────────────────────────────────────────────────────────

def parse_fields(fields: Optional[str], columns: dict) -> list[str]:
    """`fields=a,b,c` → validated column-name list (dataset order preserved)."""
    if not fields:
        return list(columns.keys())
    requested = {f.strip() for f in fields.split(",") if f.strip()}
    unknown = requested - columns.keys()
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown fields: {', '.join(sorted(unknown))}. "
                   f"Available: {', '.join(columns.keys())}",
        )
    return [name for name in columns if name in requested]


# ── Cursor (keyset) pagination ───────────────────────────────────────────────
# Cursor encodes (timestamp, id) of the last row seen. Keyset instead of
# OFFSET because scripted clients walk EVERY page and the big tables have no
# efficient deep-offset path.

def encode_cursor(ts: datetime, row_id: int) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{row_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        ts_raw, id_raw = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(ts_raw), int(id_raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=422, detail="Invalid cursor")


def apply_keyset(stmt: Select, ts_col, id_col, order: str, cursor: Optional[str]) -> Select:
    """Order by (timestamp, id) and seek past the cursor position."""
    if order == "asc":
        stmt = stmt.order_by(ts_col.asc(), id_col.asc())
        if cursor:
            ts, row_id = decode_cursor(cursor)
            stmt = stmt.where(tuple_(ts_col, id_col) > (ts, row_id))
    else:
        stmt = stmt.order_by(ts_col.desc(), id_col.desc())
        if cursor:
            ts, row_id = decode_cursor(cursor)
            stmt = stmt.where(tuple_(ts_col, id_col) < (ts, row_id))
    return stmt


# ── Statement builders ───────────────────────────────────────────────────────

def sensor_readings_stmt(
    allowed: Optional[set[int]],
    field_names: list[str],
    device_uuids: Optional[Sequence[str]] = None,
    regions: Optional[Sequence[str]] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> Select:
    stmt = (
        select(*[SENSOR_READING_COLUMNS[f] for f in field_names])
        .join(Device, SensorDeviceReading.device_id == Device.id)
    )
    stmt = apply_scope(stmt, allowed)
    if device_uuids:
        stmt = stmt.where(Device.device_uuid.in_(device_uuids))
    if regions:
        stmt = stmt.where(Device.region.in_(regions))
    if start_date:
        stmt = stmt.where(SensorDeviceReading.timestamp >= start_date)
    if end_date:
        stmt = stmt.where(SensorDeviceReading.timestamp <= end_date)
    return stmt


def mosquito_events_stmt(
    allowed: Optional[set[int]],
    field_names: list[str],
    device_uuids: Optional[Sequence[str]] = None,
    regions: Optional[Sequence[str]] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    species: Optional[str] = None,
    genus: Optional[str] = None,
    sex: Optional[str] = None,
    age_group: Optional[str] = None,
) -> Select:
    # Event ⟕ individual reading (true 1:1 via unique batch_id) ⟗ device, as
    # one flat row. Explicit joins — never the lazy-loading device_uuid
    # property on the ORM model, which costs two queries per row.
    stmt = (
        select(*[MOSQUITO_EVENT_COLUMNS[f] for f in field_names])
        .join(Device, MosquitoEvent.device_id == Device.id)
        .outerjoin(
            MosquitoIndividualReading,
            MosquitoIndividualReading.batch_id == MosquitoEvent.id,
        )
    )
    stmt = apply_scope(stmt, allowed)
    if device_uuids:
        stmt = stmt.where(Device.device_uuid.in_(device_uuids))
    if regions:
        stmt = stmt.where(Device.region.in_(regions))
    if start_date:
        stmt = stmt.where(MosquitoEvent.timestamp >= start_date)
    if end_date:
        stmt = stmt.where(MosquitoEvent.timestamp <= end_date)
    if species:
        stmt = stmt.where(MosquitoIndividualReading.species.ilike(f"%{species}%"))
    if genus:
        stmt = stmt.where(MosquitoIndividualReading.genus.ilike(f"%{genus}%"))
    if sex:
        stmt = stmt.where(MosquitoIndividualReading.sex == sex)
    if age_group:
        stmt = stmt.where(MosquitoIndividualReading.age_group == age_group)
    return stmt


def devices_stmt(
    allowed: Optional[set[int]],
    field_names: list[str],
    regions: Optional[Sequence[str]] = None,
    search: Optional[str] = None,
) -> Select:
    stmt = select(*[DEVICE_COLUMNS[f] for f in field_names])
    stmt = apply_scope(stmt, allowed)
    if regions:
        stmt = stmt.where(Device.region.in_(regions))
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            Device.name.ilike(like)
            | Device.device_uuid.ilike(like)
            | Device.region.ilike(like)
            | Device.community.ilike(like)
        )
    return stmt


def clusters_stmt(allowed: Optional[set[int]], field_names: list[str]) -> Select:
    stmt = select(*[CLUSTER_COLUMNS[f] for f in field_names])
    if allowed is not None:
        stmt = stmt.where(DeviceCluster.id.in_(allowed))
    return stmt


# ── Row serialization ────────────────────────────────────────────────────────

def serialize_value(value):
    # DB timestamps are naive UTC; make that explicit for external consumers.
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    return value


def row_to_dict(row, field_names: list[str]) -> dict:
    return {name: serialize_value(value) for name, value in zip(field_names, row)}
