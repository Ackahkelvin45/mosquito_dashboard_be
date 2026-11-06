"""Data-quality report — per-device completeness, range and silence figures.

Separate router from app/monitoring/routes.py on purpose: System Health is
SUPER_ADMIN-only at router level, while this report is also for cluster
ADMINs, scoped to their own cluster (users.cluster_id). Read-only: it flags,
it never alters or fills a value.

Every figure is a handful of GROUP BY queries over the whole scope (no
per-device queries). Only live rows count (LIVE_SENSOR_READING /
LIVE_MOSQUITO_EVENT); test-mode readings are reported separately.
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, extract, func, or_
from sqlalchemy.orm import Session

from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.core.security.permissions import is_super_admin, require_admin
from app.device.models import (
    LIVE_MOSQUITO_EVENT,
    LIVE_SENSOR_READING,
    Device,
    MosquitoEvent,
    MosquitoIndividualReading,
    SensorDeviceReading,
)
from app.device.schema import OFFLINE_AFTER_MIN
from app.monitoring.schema import DataQualityDevice, DataQualityReport
from app.notification.alert_settings import get_thresholds

router = APIRouter()

# Sensor columns reported in null_pct, keyed by the name the API returns.
NULL_FIELDS = {
    "external_temperature": SensorDeviceReading.external_temperature,
    "internal_temperature": SensorDeviceReading.internal_temperature,
    "external_humidity": SensorDeviceReading.external_humidity,
    "internal_humidity": SensorDeviceReading.internal_humidity,
    "external_pressure": SensorDeviceReading.external_pressure,
    "internal_pressure": SensorDeviceReading.internal_pressure,
    "external_light": SensorDeviceReading.external_light,
    "battery_voltage": SensorDeviceReading.battery_voltage,
    "battery_pct": SensorDeviceReading.battery_pct,
    "esp1_link_alive": SensorDeviceReading.esp1_link_alive,
}


def _count_if(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def report_scope(user: UserResponse) -> Optional[set[int]]:
    """None = every cluster (super admin). A cluster admin sees only their own
    cluster — not public clusters: data quality is a management view, not
    shared surveillance data."""
    if is_super_admin(user):
        return None
    return {user.cluster_id} if user.cluster_id is not None else set()


def build_report(session: Session, scope: Optional[set[int]], days: int,
                 now: Optional[datetime] = None,
                 start: Optional[datetime] = None) -> DataQualityReport:
    """`start` overrides now - days (used by /reports for a calendar period)."""
    window_end = now or datetime.utcnow()
    window_start = start or window_end - timedelta(days=days)
    limits = get_thresholds(session)

    device_q = session.query(Device)
    if scope is not None:
        device_q = device_q.filter(Device.cluster_id.in_(scope))
    devices = device_q.order_by(Device.name, Device.id).all()

    report = DataQualityReport(
        days=days, window_start=window_start, window_end=window_end,
        offline_threshold_min=OFFLINE_AFTER_MIN,
        temp_min=limits.temp_min, temp_max=limits.temp_max,
        humidity_min=limits.humidity_min, humidity_max=limits.humidity_max,
    )
    if not devices:
        return report
    ids = [d.id for d in devices]

    in_window = and_(
        SensorDeviceReading.device_id.in_(ids),
        SensorDeviceReading.timestamp >= window_start,
        SensorDeviceReading.timestamp <= window_end,
    )

    # 1. Completeness + range (same external→internal fallback as events.py).
    temperature = func.coalesce(SensorDeviceReading.external_temperature,
                                SensorDeviceReading.internal_temperature)
    humidity = func.coalesce(SensorDeviceReading.external_humidity,
                             SensorDeviceReading.internal_humidity)
    sensor_rows = (
        session.query(
            SensorDeviceReading.device_id,
            func.count(SensorDeviceReading.id),
            func.max(SensorDeviceReading.timestamp),
            _count_if(or_(temperature < limits.temp_min, temperature > limits.temp_max)),
            _count_if(or_(humidity < limits.humidity_min, humidity > limits.humidity_max)),
            _count_if(or_(SensorDeviceReading.battery_pct < 0, SensorDeviceReading.battery_pct > 100)),
            *[func.count(col) for col in NULL_FIELDS.values()],
        )
        .filter(in_window, LIVE_SENSOR_READING)
        .group_by(SensorDeviceReading.device_id)
        .all()
    )
    sensor = {row[0]: row[1:] for row in sensor_rows}

    # 2. Test-mode readings, reported but never mixed into the figures above.
    test_counts = dict(
        session.query(SensorDeviceReading.device_id, func.count(SensorDeviceReading.id))
        .filter(in_window, SensorDeviceReading.is_test.is_(True))
        .group_by(SensorDeviceReading.device_id)
        .all()
    )

    # 3. Silences between consecutive live readings. LAG works on both
    # Postgres and SQLite >= 3.25. Uses the device-supplied timestamp, so a
    # skewed device clock skews this too. Silence before the first reading in
    # the window is not counted; the ongoing silence after the last one is
    # added per device below.
    ts = extract("epoch", SensorDeviceReading.timestamp)
    lagged = (
        session.query(
            SensorDeviceReading.device_id.label("device_id"),
            (ts - func.lag(ts).over(
                partition_by=SensorDeviceReading.device_id,
                order_by=(SensorDeviceReading.timestamp, SensorDeviceReading.id),
            )).label("gap"),
        )
        .filter(in_window, LIVE_SENSOR_READING)
        .subquery()
    )
    threshold_s = OFFLINE_AFTER_MIN * 60
    gaps = {
        device_id: (longest, over)
        for device_id, longest, over in session.query(
            lagged.c.device_id,
            func.max(lagged.c.gap),
            _count_if(lagged.c.gap > threshold_s),
        )
        .filter(lagged.c.gap.isnot(None))
        .group_by(lagged.c.device_id)
        .all()
    }

    # 3b. Delivery delay: server receive time minus device timestamp, over
    # live readings that have received_at (NULL = before the column existed,
    # or CSV-imported: not measured, never 0). Negative = device clock ahead.
    delay = extract("epoch", SensorDeviceReading.received_at) - ts
    delays = {
        row[0]: row[1:]
        for row in session.query(
            SensorDeviceReading.device_id,
            func.count(SensorDeviceReading.id),
            func.avg(delay), func.min(delay), func.max(delay),
            _count_if(delay > threshold_s),
        )
        .filter(in_window, LIVE_SENSOR_READING, SensorDeviceReading.received_at.isnot(None))
        .group_by(SensorDeviceReading.device_id)
        .all()
    }

    # 4. Detections. Outer join: an event without its reading row counts as
    # missing p_mosq and genus.
    genus_missing = or_(MosquitoIndividualReading.genus.is_(None),
                        func.trim(MosquitoIndividualReading.genus) == "")
    p = MosquitoIndividualReading.p_mosq
    detection_rows = (
        session.query(
            MosquitoEvent.device_id,
            func.count(MosquitoEvent.id),
            _count_if(p.is_(None)),
            _count_if(genus_missing),
            _count_if(or_(p < 0, p > 1)),
        )
        .outerjoin(MosquitoIndividualReading, MosquitoIndividualReading.batch_id == MosquitoEvent.id)
        .filter(
            MosquitoEvent.device_id.in_(ids),
            MosquitoEvent.timestamp >= window_start,
            MosquitoEvent.timestamp <= window_end,
            LIVE_MOSQUITO_EVENT,
        )
        .group_by(MosquitoEvent.device_id)
        .all()
    )
    detections = {row[0]: row[1:] for row in detection_rows}

    for d in devices:
        total, last_at, temp_oor, hum_oor, batt_oor, *non_null = sensor.get(d.id, (0, None, 0, 0, 0))
        longest, over = gaps.get(d.id, (None, None))
        if longest is not None:
            longest = float(longest)  # Postgres EXTRACT returns numeric
        # A device that has gone quiet is still in a silence right now.
        ongoing = (window_end - last_at).total_seconds() if last_at else None
        if ongoing is not None and ongoing > threshold_s:
            over = (over or 0) + 1
            longest = max(longest or 0.0, ongoing)
        measured, avg_d, min_d, max_d, delayed = delays.get(d.id, (0, None, None, None, 0))
        delay_s = lambda v: round(float(v), 1) if v is not None else None  # noqa: E731
        det_total, det_no_p, det_no_genus, det_p_oor = detections.get(d.id, (0, 0, 0, 0))
        report.devices.append(DataQualityDevice(
            device_id=d.id, device_uuid=d.device_uuid, name=d.name,
            cluster_id=d.cluster_id, region=d.region,
            total_readings=total,
            test_readings=test_counts.get(d.id, 0),
            last_reading_at=last_at,
            null_pct=(
                {name: round(100 * (total - present) / total, 1)
                 for name, present in zip(NULL_FIELDS, non_null)}
                if total else dict.fromkeys(NULL_FIELDS)
            ),
            temperature_out_of_range=temp_oor,
            humidity_out_of_range=hum_oor,
            battery_pct_out_of_range=batt_oor,
            longest_silence_min=round(longest / 60, 1) if longest is not None else None,
            silences_over_threshold=over,
            delay_measured=measured,
            avg_delay_s=delay_s(avg_d),
            min_delay_s=delay_s(min_d),
            max_delay_s=delay_s(max_d),
            delayed_over_threshold=delayed,
            detections=det_total,
            detections_missing_p_mosq=det_no_p,
            detections_missing_genus=det_no_genus,
            detections_p_mosq_out_of_range=det_p_oor,
        ))
    return report


@router.get("/data-quality", response_model=DataQualityReport)
def data_quality(
    days: int = Query(default=7, ge=1, le=90),
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(require_admin),
):
    return build_report(session, report_scope(current_user), days)
