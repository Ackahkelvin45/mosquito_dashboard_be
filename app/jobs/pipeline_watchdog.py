"""Fleet-silence watchdog — catches the failure the per-device offline
detector can't see: EVERY trap quiet at once, which almost always means the
broker or the server's own connectivity died, not fifty simultaneous device
failures.

Judged on Device.last_activity (any message), not the sensor_data heartbeat:
if even sporadic mosquito_data is arriving, the pipeline itself is alive and
the per-device offline job is the right alarm instead.

Alerting is deduped by the notification service (dedupe_key="pipeline_silent",
60 min window) rather than a state machine — a broker outage is rare and an
hourly reminder while it lasts is a feature.
"""
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import func

from app.core.database import SessionLocal
from app.device.models import Device
from app.notification.events import NotificationEvent, emit

logger = logging.getLogger(__name__)

MONITOR_FLEET_SILENCE_MIN = int(os.getenv("MONITOR_FLEET_SILENCE_MIN", "30"))


def run_pipeline_watchdog() -> None:
    with SessionLocal() as session:
        device_count = int(session.query(func.count(Device.id)).scalar() or 0)
        if device_count == 0:
            return  # nothing deployed, nothing to watch

        last_any = session.query(func.max(Device.last_activity)).scalar()
        if last_any is None:
            return

        now = datetime.utcnow()
        if last_any >= now - timedelta(minutes=MONITOR_FLEET_SILENCE_MIN):
            return

        silent_minutes = round((now - last_any).total_seconds() / 60)
        emit(session, NotificationEvent.PIPELINE_SILENT,
             silent_minutes=silent_minutes, device_count=device_count)
        logger.warning(
            "Pipeline silent: no MQTT message from any of %d device(s) for ~%d min",
            device_count, silent_minutes,
        )
