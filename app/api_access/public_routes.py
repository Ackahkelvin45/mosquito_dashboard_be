"""Public data API v1 — read-only, API-key authenticated (X-API-Key header).

Scope: every endpoint filters by the key OWNER's visible clusters via the same
visible_cluster_ids() the app uses; an explicit cluster_id filter is
INTERSECTED with that scope (out-of-scope requests yield empty results, never
an error that would confirm the cluster exists).

Pagination: the two high-volume tables (sensor-readings, mosquito-events) use
keyset cursors — stable under concurrent inserts and O(1) at any depth. The
small tables (devices, clusters) use the app's standard page/page_size.
"""
import csv
import io
import json
import threading
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api_access.auth import get_api_key_principal, get_api_key_principal_export
from app.api_access.queries import (
    CLUSTER_COLUMNS,
    DEVICE_COLUMNS,
    DEVICE_SORT_COLUMNS,
    MOSQUITO_EVENT_COLUMNS,
    SENSOR_READING_COLUMNS,
    apply_keyset,
    clusters_stmt,
    devices_stmt,
    encode_cursor,
    mosquito_events_stmt,
    parse_fields,
    resolve_scope,
    row_to_dict,
    sensor_readings_stmt,
    serialize_value,
)
from app.core.database import SessionLocal, get_db
from app.core.pagination import Page
from app.device.models import Device, DeviceCluster, MosquitoEvent, SensorDeviceReading

router = APIRouter(tags=["public-api-v1"])

MAX_LIMIT = 500
DEFAULT_LIMIT = 100

# ponytail: soft cap — the pre-check in the route and the increment in the
# generator aren't atomic, so a simultaneous burst can briefly exceed it.
# Good enough as an abuse guard; a hard cap needs a real queue.
MAX_CONCURRENT_EXPORTS = 2
_exports_lock = threading.Lock()
_active_exports = 0


# ── Cursor-paginated datasets ────────────────────────────────────────────────

@router.get("/sensor-readings", summary="Sensor readings (cursor-paginated)")
def list_sensor_readings(
    session: Session = Depends(get_db),
    principal=Depends(get_api_key_principal),
    device_uuid: Optional[List[str]] = Query(default=None, description="Repeatable device UUID filter"),
    cluster_id: Optional[List[int]] = Query(default=None, description="Repeatable cluster filter (intersected with your scope)"),
    region: Optional[List[str]] = Query(default=None, description="Repeatable region filter"),
    start_date: Optional[datetime] = Query(default=None, description="Inclusive UTC lower bound"),
    end_date: Optional[datetime] = Query(default=None, description="Inclusive UTC upper bound"),
    order: Literal["asc", "desc"] = Query(default="desc", description="By timestamp"),
    fields: Optional[str] = Query(default=None, description="Comma-separated subset of columns"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = Query(default=None, description="next_cursor from the previous page"),
):
    user, _ = principal
    allowed = resolve_scope(session, user, cluster_id)
    field_names = parse_fields(fields, SENSOR_READING_COLUMNS)
    stmt = sensor_readings_stmt(
        allowed, field_names,
        device_uuids=device_uuid, regions=region,
        start_date=start_date, end_date=end_date,
    )
    # Keyset columns are always selected internally; fields= only trims output.
    stmt = stmt.add_columns(SensorDeviceReading.timestamp, SensorDeviceReading.id)
    stmt = apply_keyset(stmt, SensorDeviceReading.timestamp, SensorDeviceReading.id, order, cursor)
    rows = session.execute(stmt.limit(limit + 1)).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [row_to_dict(row[: len(field_names)], field_names) for row in rows]
    next_cursor = encode_cursor(rows[-1][-2], rows[-1][-1]) if has_more else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/mosquito-events", summary="Mosquito events with species detail (cursor-paginated)")
def list_mosquito_events(
    session: Session = Depends(get_db),
    principal=Depends(get_api_key_principal),
    device_uuid: Optional[List[str]] = Query(default=None),
    cluster_id: Optional[List[int]] = Query(default=None),
    region: Optional[List[str]] = Query(default=None),
    start_date: Optional[datetime] = Query(default=None),
    end_date: Optional[datetime] = Query(default=None),
    species: Optional[str] = Query(default=None, description="Substring match"),
    genus: Optional[str] = Query(default=None, description="Substring match"),
    sex: Optional[str] = Query(default=None, description="Exact match"),
    age_group: Optional[str] = Query(default=None, description="Exact match"),
    order: Literal["asc", "desc"] = Query(default="desc"),
    fields: Optional[str] = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = Query(default=None),
):
    user, _ = principal
    allowed = resolve_scope(session, user, cluster_id)
    field_names = parse_fields(fields, MOSQUITO_EVENT_COLUMNS)
    stmt = mosquito_events_stmt(
        allowed, field_names,
        device_uuids=device_uuid, regions=region,
        start_date=start_date, end_date=end_date,
        species=species, genus=genus, sex=sex, age_group=age_group,
    )
    stmt = stmt.add_columns(MosquitoEvent.timestamp, MosquitoEvent.id)
    stmt = apply_keyset(stmt, MosquitoEvent.timestamp, MosquitoEvent.id, order, cursor)
    rows = session.execute(stmt.limit(limit + 1)).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [row_to_dict(row[: len(field_names)], field_names) for row in rows]
    next_cursor = encode_cursor(rows[-1][-2], rows[-1][-1]) if has_more else None
    return {"items": items, "next_cursor": next_cursor}


# ── Page-paginated datasets (small tables) ───────────────────────────────────

@router.get("/devices", response_model=Page[dict], summary="Devices (page-paginated)")
def list_devices(
    session: Session = Depends(get_db),
    principal=Depends(get_api_key_principal),
    cluster_id: Optional[List[int]] = Query(default=None),
    region: Optional[List[str]] = Query(default=None),
    search: Optional[str] = Query(default=None, description="Matches name, UUID, region, community"),
    sort: Literal["created_at", "name", "last_activity", "total_mosquito_count"] = Query(default="created_at"),
    order: Literal["asc", "desc"] = Query(default="desc"),
    fields: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
):
    user, _ = principal
    allowed = resolve_scope(session, user, cluster_id)
    field_names = parse_fields(fields, DEVICE_COLUMNS)
    stmt = devices_stmt(allowed, field_names, regions=region, search=search)

    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    sort_col = DEVICE_SORT_COLUMNS[sort]
    stmt = stmt.order_by(sort_col.asc() if order == "asc" else sort_col.desc(), Device.id.desc())
    rows = session.execute(stmt.offset((page - 1) * page_size).limit(page_size)).all()

    items = [row_to_dict(row, field_names) for row in rows]
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return Page[dict](items=items, total=total, page=page, page_size=page_size, total_pages=total_pages)


@router.get("/clusters", response_model=Page[dict], summary="Device clusters visible to your key")
def list_clusters(
    session: Session = Depends(get_db),
    principal=Depends(get_api_key_principal),
    fields: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
):
    user, _ = principal
    allowed = resolve_scope(session, user, None)
    field_names = parse_fields(fields, CLUSTER_COLUMNS)
    stmt = clusters_stmt(allowed, field_names)

    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    rows = session.execute(
        stmt.order_by(DeviceCluster.id.asc()).offset((page - 1) * page_size).limit(page_size)
    ).all()

    items = [row_to_dict(row, field_names) for row in rows]
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return Page[dict](items=items, total=total, page=page, page_size=page_size, total_pages=total_pages)


# ── Export (streaming) ───────────────────────────────────────────────────────

def _csv_escape(value):
    # Excel formula-injection guard: species/genus/... are device-supplied
    # strings; a cell starting with = + - @ executes when the CSV is opened.
    if isinstance(value, str) and value[:1] in "=+-@":
        return "'" + value
    if value is None:
        return ""
    return serialize_value(value)


@router.get(
    "/export",
    summary="Stream a full filtered dataset as CSV or JSONL",
    description="Same filters as the matching list endpoint. Rows stream oldest-first; "
                "the response is generated incrementally, so arbitrarily large ranges "
                "download in constant memory.",
)
def export_data(
    session: Session = Depends(get_db),
    principal=Depends(get_api_key_principal_export),
    dataset: Literal["sensor-readings", "mosquito-events", "devices"] = Query(...),
    format: Literal["csv", "jsonl"] = Query(default="csv"),
    device_uuid: Optional[List[str]] = Query(default=None),
    cluster_id: Optional[List[int]] = Query(default=None),
    region: Optional[List[str]] = Query(default=None),
    start_date: Optional[datetime] = Query(default=None),
    end_date: Optional[datetime] = Query(default=None),
    species: Optional[str] = Query(default=None),
    genus: Optional[str] = Query(default=None),
    sex: Optional[str] = Query(default=None),
    age_group: Optional[str] = Query(default=None),
    fields: Optional[str] = Query(default=None),
):
    user, _ = principal
    allowed = resolve_scope(session, user, cluster_id)

    if dataset == "sensor-readings":
        field_names = parse_fields(fields, SENSOR_READING_COLUMNS)
        stmt = sensor_readings_stmt(
            allowed, field_names, device_uuids=device_uuid, regions=region,
            start_date=start_date, end_date=end_date,
        ).order_by(SensorDeviceReading.timestamp.asc(), SensorDeviceReading.id.asc())
    elif dataset == "mosquito-events":
        field_names = parse_fields(fields, MOSQUITO_EVENT_COLUMNS)
        stmt = mosquito_events_stmt(
            allowed, field_names, device_uuids=device_uuid, regions=region,
            start_date=start_date, end_date=end_date,
            species=species, genus=genus, sex=sex, age_group=age_group,
        ).order_by(MosquitoEvent.timestamp.asc(), MosquitoEvent.id.asc())
    else:
        field_names = parse_fields(fields, DEVICE_COLUMNS)
        stmt = devices_stmt(allowed, field_names, regions=region, search=None).order_by(Device.id.asc())

    with _exports_lock:
        if _active_exports >= MAX_CONCURRENT_EXPORTS:
            raise HTTPException(
                status_code=429,
                detail="Too many exports running; try again in a moment",
                headers={"Retry-After": "30"},
            )

    def generate():
        # The request session (get_db) is closed BEFORE a StreamingResponse
        # body runs on this FastAPI version — the generator must own its
        # session so the server-side cursor lives exactly as long as the
        # stream, including on client disconnect.
        global _active_exports
        with _exports_lock:
            _active_exports += 1
        try:
            with SessionLocal() as stream_session:
                result = stream_session.execute(
                    stmt.execution_options(yield_per=1000)
                )
                if format == "csv":
                    buffer = io.StringIO()
                    writer = csv.writer(buffer)

                    def flush_row(values):
                        writer.writerow(values)
                        chunk = buffer.getvalue()
                        buffer.seek(0)
                        buffer.truncate(0)
                        return chunk

                    yield flush_row(field_names)
                    for row in result:
                        yield flush_row([_csv_escape(v) for v in row])
                else:
                    for row in result:
                        yield json.dumps(row_to_dict(row, field_names)) + "\n"
        finally:
            with _exports_lock:
                _active_exports -= 1

    stamp = datetime.utcnow().strftime("%Y%m%d")
    range_part = ""
    if start_date or end_date:
        range_part = f"_{start_date:%Y%m%d}" if start_date else "_"
        range_part += f"-{end_date:%Y%m%d}" if end_date else ""
    filename = f"{dataset}{range_part or '_' + stamp}.{format}"
    media_type = "text/csv" if format == "csv" else "application/x-ndjson"
    return StreamingResponse(
        generate(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
