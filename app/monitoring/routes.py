"""System Health endpoints — SUPER_ADMIN only.

Read-only views over the ingest instrumentation (app/monitoring) plus live
broker state, powering the frontend's System Health page so nobody has to
tail server logs to know whether the pipeline is alive.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pagination import DEFAULT_PAGE, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.core.security.permissions import require_super_admin
from app.device.models import Device
from app.device.schema import OFFLINE_AFTER_MIN
from app.monitoring import recorder
from app.monitoring.models import MqttIngestError, MqttTrafficHourly
from app.monitoring.schema import (
    MqttErrorResponse,
    MqttStatusResponse,
    SilentDevice,
    TrafficBucket,
)

router = APIRouter(dependencies=[Depends(require_super_admin)])

MAX_TRAFFIC_HOURS = 24 * 30  # bounded by the 30-day retention anyway


def _traffic_buckets(session: Session, hours: int,
                     device_id: int | None = None) -> list[TrafficBucket]:
    """Hourly buckets over the window, zero-filled so charts have no gaps."""
    now_bucket = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    start = now_bucket - timedelta(hours=hours - 1)

    q = (
        session.query(
            MqttTrafficHourly.bucket_start,
            MqttTrafficHourly.event_type,
            MqttTrafficHourly.is_test,
            func.sum(MqttTrafficHourly.message_count),
        )
        .filter(MqttTrafficHourly.bucket_start >= start)
        .group_by(MqttTrafficHourly.bucket_start,
                  MqttTrafficHourly.event_type,
                  MqttTrafficHourly.is_test)
    )
    if device_id is not None:
        q = q.filter(MqttTrafficHourly.device_id == device_id)

    by_bucket: dict[datetime, TrafficBucket] = {}
    cursor = start
    while cursor <= now_bucket:
        by_bucket[cursor] = TrafficBucket(bucket_start=cursor)
        cursor += timedelta(hours=1)

    for bucket_start, event_type, is_test, count in q.all():
        row = by_bucket.get(bucket_start)
        if row is None:  # clock skew edge — don't lose the data, don't crash
            row = by_bucket[bucket_start] = TrafficBucket(bucket_start=bucket_start)
        if is_test:
            row.test_count += int(count)
        elif event_type == "sensor_data":
            row.sensor_count += int(count)
        else:
            row.mosquito_count += int(count)

    return [by_bucket[k] for k in sorted(by_bucket)]


@router.get("/mqtt/status", response_model=MqttStatusResponse)
def mqtt_status(session: Session = Depends(get_db)):
    now = datetime.utcnow()
    day_ago = now - timedelta(hours=24)
    threshold = now - timedelta(minutes=OFFLINE_AFTER_MIN)

    messages_24h = int(
        session.query(func.coalesce(func.sum(MqttTrafficHourly.message_count), 0))
        .filter(MqttTrafficHourly.bucket_start >= day_ago - timedelta(hours=1))
        .scalar() or 0
    )
    errors_24h = int(
        session.query(func.count(MqttIngestError.id))
        .filter(MqttIngestError.occurred_at >= day_ago)
        .scalar() or 0
    )
    devices_total = int(session.query(func.count(Device.id)).scalar() or 0)
    devices_reporting = int(
        session.query(func.count(Device.id))
        .filter(Device.last_sensor_data_at >= threshold)
        .scalar() or 0
    )
    silent = (
        session.query(Device)
        .filter((Device.last_sensor_data_at < threshold)
                | (Device.last_sensor_data_at.is_(None)))
        .order_by(Device.last_sensor_data_at.desc().nullslast())
        .limit(10)
        .all()
    )

    # After a restart the in-memory marker is empty; the fleet's latest
    # activity is the best persisted approximation of "last message".
    last_message_at = recorder.broker_state["last_message_at"]
    if last_message_at is None:
        last_message_at = session.query(func.max(Device.last_activity)).scalar()

    return MqttStatusResponse(
        broker_connected=recorder.broker_state["connected"],
        broker_state_since=recorder.broker_state["since"],
        last_message_at=last_message_at,
        messages_24h=messages_24h,
        errors_24h=errors_24h,
        devices_total=devices_total,
        devices_reporting=devices_reporting,
        silent_devices=[
            SilentDevice(id=d.id, name=d.name, last_sensor_data_at=d.last_sensor_data_at)
            for d in silent
        ],
        offline_threshold_min=OFFLINE_AFTER_MIN,
    )


@router.get("/mqtt/traffic", response_model=list[TrafficBucket])
def mqtt_traffic(
    hours: int = Query(default=24, ge=1, le=MAX_TRAFFIC_HOURS),
    device_id: int | None = Query(default=None),
    session: Session = Depends(get_db),
):
    return _traffic_buckets(session, hours, device_id)


@router.get("/mqtt/devices/{device_id}/timeline", response_model=list[TrafficBucket])
def device_timeline(
    device_id: int,
    hours: int = Query(default=48, ge=1, le=MAX_TRAFFIC_HOURS),
    session: Session = Depends(get_db),
):
    if not session.get(Device, device_id):
        raise HTTPException(status_code=404, detail="Device not found")
    return _traffic_buckets(session, hours, device_id)


@router.get("/mqtt/errors", response_model=Page[MqttErrorResponse])
def mqtt_errors(
    page: int = Query(default=DEFAULT_PAGE, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    error_type: str | None = Query(default=None),
    session: Session = Depends(get_db),
):
    q = session.query(MqttIngestError)
    if error_type:
        q = q.filter(MqttIngestError.error_type == error_type)
    total = q.count()
    rows = (
        q.order_by(MqttIngestError.occurred_at.desc(), MqttIngestError.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return Page(
        items=[MqttErrorResponse.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size, total_pages=total_pages,
    )
