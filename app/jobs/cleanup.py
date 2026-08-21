"""Hourly notification cleanup: purge expired rows and soft-deleted rows
older than 30 days (both handled inside NotificationService.delete_expired).
Also prunes the MQTT monitoring tables to their retention windows."""
import logging
import os
from datetime import datetime, timedelta

from app.audit.models import AuditLog
from app.core.database import SessionLocal
from app.monitoring.models import MqttIngestError, MqttTrafficHourly
from app.notification.service import NotificationService

logger = logging.getLogger(__name__)

# System Health retention: 30 days of hourly traffic, 7 days of raw errors.
MONITOR_TRAFFIC_RETENTION_DAYS = int(os.getenv("MONITOR_TRAFFIC_RETENTION_DAYS", "30"))
MONITOR_ERROR_RETENTION_DAYS = int(os.getenv("MONITOR_ERROR_RETENTION_DAYS", "7"))
# Audit retention: spec requires >= 12 months; default keeps 24.
AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "730"))


def run_notification_cleanup() -> None:
    with SessionLocal() as session:
        # delete_expired already logs the expired/purged split; log the total.
        removed = NotificationService(session).delete_expired()
        if removed:
            logger.info("Notification cleanup removed %d row(s)", removed)


def run_monitoring_cleanup() -> None:
    now = datetime.utcnow()
    with SessionLocal() as session:
        traffic = (
            session.query(MqttTrafficHourly)
            .filter(MqttTrafficHourly.bucket_start
                    < now - timedelta(days=MONITOR_TRAFFIC_RETENTION_DAYS))
            .delete(synchronize_session=False)
        )
        errors = (
            session.query(MqttIngestError)
            .filter(MqttIngestError.occurred_at
                    < now - timedelta(days=MONITOR_ERROR_RETENTION_DAYS))
            .delete(synchronize_session=False)
        )
        audit_rows = (
            session.query(AuditLog)
            .filter(AuditLog.occurred_at < now - timedelta(days=AUDIT_RETENTION_DAYS))
            .delete(synchronize_session=False)
        )
        session.commit()
        if traffic or errors or audit_rows:
            logger.info("Monitoring cleanup removed %d traffic bucket(s), %d error row(s), %d audit row(s)",
                        traffic, errors, audit_rows)
