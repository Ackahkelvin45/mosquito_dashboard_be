"""Device health sweep — every NOTIFY_HEALTH_CHECK_SEC (default 24h).

Two checks:
  1. Maintenance: devices with no activity for NOTIFY_MAINTENANCE_DAYS
     (default 30) → MAINTENANCE_DUE. Re-notification cadence is governed by
     events.py's maintenance dedupe window.
  2. Stuck sensors: for devices that ARE reporting, if the last
     STUCK_READING_COUNT readings carry identical sensor values the sensor is
     probably frozen → SENSOR_MALFUNCTION (events.py dedupes per device).
"""
import logging
import os
from datetime import datetime, timedelta

from app.core.database import SessionLocal
from app.device.models import Device, SensorDeviceReading
from app.notification.events import NotificationEvent, emit

logger = logging.getLogger(__name__)

NOTIFY_MAINTENANCE_DAYS = int(os.getenv("NOTIFY_MAINTENANCE_DAYS", "30"))
# How many identical consecutive readings count as a stuck sensor.
STUCK_READING_COUNT = 5


def run_health_sweep() -> None:
    with SessionLocal() as session:
        now = datetime.utcnow()

        # 1. Maintenance overdue.
        stale_cutoff = now - timedelta(days=NOTIFY_MAINTENANCE_DAYS)
        stale_devices = (
            session.query(Device).filter(Device.last_activity < stale_cutoff).all()
        )
        for device in stale_devices:
            days_inactive = (now - device.last_activity).days
            emit(
                session,
                NotificationEvent.MAINTENANCE_DUE,
                device=device,
                days_inactive=days_inactive,
            )

        # 2. Stuck-sensor sweep — only devices active in the last 24h (a dead
        # device is the offline/maintenance jobs' problem, not a stuck sensor).
        stuck = 0
        active_devices = (
            session.query(Device)
            .filter(Device.last_activity >= now - timedelta(hours=24))
            .all()
        )
        for device in active_devices:
            readings = (
                session.query(SensorDeviceReading)
                .filter(SensorDeviceReading.device_id == device.id)
                .order_by(
                    SensorDeviceReading.timestamp.desc(),
                    SensorDeviceReading.id.desc(),
                )
                .limit(STUCK_READING_COUNT)
                .all()
            )
            if len(readings) < STUCK_READING_COUNT:
                continue
            first = _signature(readings[0])
            if all(_signature(reading) == first for reading in readings[1:]) and any(
                value is not None for value in first
            ):
                stuck += 1
                emit(
                    session,
                    NotificationEvent.SENSOR_MALFUNCTION,
                    device=device,
                    reason=(
                        f"last {STUCK_READING_COUNT} readings are identical "
                        "(possible stuck sensor)"
                    ),
                )

        if stale_devices or stuck:
            logger.info(
                "Health sweep: %d device(s) maintenance-due, %d possible stuck sensor(s)",
                len(stale_devices), stuck,
            )


def _signature(reading: SensorDeviceReading) -> tuple:
    return (
        reading.external_temperature,
        reading.internal_temperature,
        reading.external_humidity,
        reading.internal_humidity,
        reading.battery_voltage,
    )
