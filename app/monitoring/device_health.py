"""GET /monitoring/device-health — one row per device: battery, detector link,
GPS fix, last seen and fault flags. Same access as /monitoring/data-quality
(ADMIN own cluster, SUPER_ADMIN all) and built on its report, so the 7-day
clock-ahead and invalid-value counts are the same numbers. Every flag comes
from data the device sent; nothing is estimated."""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.core.security.permissions import require_admin
from app.device.models import DEVICE_SENT_SENSOR_READING, LIVE_SENSOR_READING, Device, SensorDeviceReading
from app.device.schema import OFFLINE_AFTER_MIN, is_online
from app.monitoring.data_quality import CLOCK_AHEAD_TOLERANCE, build_report, report_scope
from app.monitoring.schema import DeviceHealth, DeviceHealthReport, HealthFault
from app.notification.alert_settings import get_thresholds
from utils.coordinates import normalize_lat_lon

router = APIRouter()

DAYS = 7
R = SensorDeviceReading
# sensor_missing needs BOTH sensors of a quantity silent: builds without an
# external sensor still report the internal one and must not be flagged forever.
CORE_SENSORS = {
    "temperature": (R.external_temperature, R.internal_temperature),
    "humidity": (R.external_humidity, R.internal_humidity),
}


def gps_fix(device: Device) -> Optional[bool]:
    """True = usable stored fix, False = stored but unusable (e.g. 0,0), None = no position."""
    if device.latitude is None and device.longitude is None:
        return None
    return normalize_lat_lon(device.latitude, device.longitude)[0] is not None


def _latest_reading(session: Session, device_id: int, now: datetime) -> Optional[SensorDeviceReading]:
    """Newest live, device-sent, non-excluded reading — skipping rows stamped in
    the future (a clock running ahead must not pin an old value as "latest")."""
    # ponytail: one LIMIT-1 query per device (like jobs/summaries.py); batch it if fleets grow to hundreds.
    return (
        session.query(R)
        .filter(R.device_id == device_id, LIVE_SENSOR_READING, DEVICE_SENT_SENSOR_READING,
                R.excluded_at.is_(None), R.timestamp <= now + CLOCK_AHEAD_TOLERANCE)
        .order_by(R.timestamp.desc(), R.id.desc())
        .first()
    )


@router.get("/device-health", response_model=DeviceHealthReport)
def device_health(
    device_id: Optional[int] = Query(None, description="Only this device (empty list if outside your scope)"),
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(require_admin),
):
    now = datetime.utcnow()
    quality = build_report(session, report_scope(current_user), DAYS, now=now, device_id=device_id).devices
    limits = get_thresholds(session)
    report = DeviceHealthReport(days=DAYS, offline_threshold_min=OFFLINE_AFTER_MIN,
                                battery_low_v=limits.battery_critical_v,
                                battery_low_pct=limits.battery_critical_pct)
    if not quality:
        return report

    # Device-sent readings only (CSV imports say nothing about the hardware).
    ids = [q.device_id for q in quality]
    sensor_counts = {
        row[0]: row[1:]
        for row in session.query(R.device_id, func.count(R.id),
                                 *[func.count(col) for pair in CORE_SENSORS.values() for col in pair])
        .filter(R.device_id.in_(ids), LIVE_SENSOR_READING, DEVICE_SENT_SENSOR_READING,
                R.timestamp >= now - timedelta(days=DAYS), R.timestamp <= now)
        .group_by(R.device_id)
        .all()
    }

    for q in quality:
        d = session.get(Device, q.device_id)  # already in the identity map from build_report
        r = _latest_reading(session, d.id, now)
        volts = r.battery_voltage if r else None
        pct = r.battery_pct if r else None
        if pct is not None and not 0 <= pct <= 100:
            pct = None  # impossible value: not a reading (counted by Data Quality)
        link = r.esp1_link_alive if r else None
        gps = gps_fix(d)
        online = is_online(d.last_sensor_data_at)

        faults = []
        flag = lambda code, label, detail: faults.append(HealthFault(code=code, label=label, detail=detail))  # noqa: E731
        if d.last_sensor_data_at is None:
            flag("never_reported", "Never reported", "No sensor data has ever been received from this device.")
        elif not online:
            flag("offline", "Offline", f"No sensor data for more than {OFFLINE_AFTER_MIN} minutes.")
        # Same rule as the low-battery alert: percentage first, voltage only without it.
        if pct is not None:
            if pct < limits.battery_critical_pct:
                flag("low_battery", "Low battery", f"Battery at {pct}% (alert below {limits.battery_critical_pct}%).")
        elif volts is not None and volts < limits.battery_critical_v:
            flag("low_battery", "Low battery", f"Battery at {volts:.2f} V (alert below {limits.battery_critical_v:g} V).")
        if link is False:
            flag("detector_disconnected", "Detector disconnected",
                 "The latest reading says the gateway has no link to the detector unit.")
        if gps is False:
            flag("no_gps_fix", "No GPS fix", "The stored position is not a usable GPS fix (for example 0,0).")
        if q.clock_ahead_count:
            flag("clock_ahead", "Clock ahead",
                 f"{q.clock_ahead_count} reading(s) in the last {DAYS} days were stamped more than "
                 f"{int(CLOCK_AHEAD_TOLERANCE.total_seconds() // 60)} minutes after they arrived.")
        if q.invalid_values_received:
            flag("invalid_values", "Invalid values",
                 f"{q.invalid_values_received} impossible value(s) received in the last {DAYS} days "
                 "(test-mode messages included), stored as blank.")
        total, *present = sensor_counts.get(d.id, (0,))
        if total:
            missing = [name for i, name in enumerate(CORE_SENSORS) if not present[2 * i] and not present[2 * i + 1]]
            if missing:
                flag("sensor_missing", "Sensor missing",
                     f"No {' or '.join(missing)} value from either sensor (outside or inside) in any of the "
                     f"{total} reading(s) the device sent in the last {DAYS} days.")

        report.devices.append(DeviceHealth(
            id=d.id, device_uuid=d.device_uuid, name=d.name, cluster_id=d.cluster_id,
            cluster_name=d.cluster.name if d.cluster else None, region=d.region,
            last_seen=d.last_sensor_data_at, last_activity=d.last_activity, is_active=online,
            reading_at=r.timestamp if r else None,
            battery_voltage=volts, battery_pct=pct, detector_link=link, gps_fix=gps, faults=faults,
        ))
    return report
