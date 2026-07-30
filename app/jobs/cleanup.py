"""Hourly notification cleanup: purge expired rows and soft-deleted rows
older than 30 days (both handled inside NotificationService.delete_expired)."""
import logging

from app.core.database import SessionLocal
from app.notification.service import NotificationService

logger = logging.getLogger(__name__)


def run_notification_cleanup() -> None:
    with SessionLocal() as session:
        # delete_expired already logs the expired/purged split; log the total.
        removed = NotificationService(session).delete_expired()
        if removed:
            logger.info("Notification cleanup removed %d row(s)", removed)
