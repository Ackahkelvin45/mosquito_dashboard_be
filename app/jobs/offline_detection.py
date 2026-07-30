"""Offline/online detector — THE owner of the devices.offline_since state
machine (plan §4: "state-machine via devices.offline_since").

Every NOTIFY_OFFLINE_CHECK_SEC (default 120s):
  - devices whose last_activity is older than NOTIFY_OFFLINE_AFTER_MIN
    (default 30 min) and offline_since IS NULL are stamped offline_since=now
    and a DEVICE_OFFLINE is emitted — exactly once per outage, no dedupe
    window needed.
  - devices with offline_since set whose last_activity is recent again are
    cleared and a DEVICE_ONLINE is emitted.

State is committed BEFORE emitting so a notification hiccup can never cause
repeat alerts (emit itself never raises).
"""
import logging
import os
from datetime import datetime, timedelta

from app.core.database import SessionLocal
from app.device.models import Device
from app.notification.events import NotificationEvent, emit

logger = logging.getLogger(__name__)

NOTIFY_OFFLINE_AFTER_MIN = int(os.getenv("NOTIFY_OFFLINE_AFTER_MIN", "30"))


def run_offline_detection() -> None:
    with SessionLocal() as session:
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=NOTIFY_OFFLINE_AFTER_MIN)

        newly_offline = (
            session.query(Device)
            .filter(Device.last_activity < cutoff, Device.offline_since.is_(None))
            .all()
        )
        for device in newly_offline:
            device.offline_since = now
            session.commit()
            emit(session, NotificationEvent.DEVICE_OFFLINE, device=device)
            logger.info(
                "Device %s (%s) marked offline (last activity %s)",
                device.id, device.name, device.last_activity,
            )

        recovered = (
            session.query(Device)
            .filter(Device.offline_since.isnot(None), Device.last_activity >= cutoff)
            .all()
        )
        for device in recovered:
            offline_minutes = round((now - device.offline_since).total_seconds() / 60)
            device.offline_since = None
            session.commit()
            emit(
                session,
                NotificationEvent.DEVICE_ONLINE,
                device=device,
                offline_duration_min=offline_minutes,  # extra ctx — tolerated by emit
            )
            logger.info(
                "Device %s (%s) back online after ~%s min",
                device.id, device.name, offline_minutes,
            )

        if newly_offline or recovered:
            logger.info(
                "Offline detection: %d newly offline, %d recovered",
                len(newly_offline), len(recovered),
            )
