"""Push delivery retry — every NOTIFY_PUSH_RETRY_SEC (default 600s), re-attempt
FAILED push deliveries that are still under the attempt cap.

Backoff is exponential-ish: a delivery is skipped while its last attempt is
more recent than attempts * NOTIFY_PUSH_RETRY_SEC, so each failure waits
longer than the last. Deliveries whose notification/user is gone, whose user
disabled push, or who no longer has any active subscription are marked as
failed attempts so they age out of the queue instead of being reselected
forever. Subscription-level failure counting/deactivation stays inside
PushChannel (channels/push.py) — this job reuses it unchanged.

The PUSH-channel filter is queried on the models directly: the delivery repo's
list_failed() returns both channels and the repo must not be modified.
"""
import logging
import os
from datetime import datetime, timedelta

from app.authentication.models import User
from app.core.database import SessionLocal
from app.notification.channels.push import PushChannel
from app.notification.enums import DeliveryChannel, DeliveryStatus
from app.notification.models import Notification, NotificationDelivery
from app.notification.repository.notification_repository import (
    NotificationDeliveryRepository,
    NotificationPreferenceRepository,
    PushSubscriptionRepository,
)

logger = logging.getLogger(__name__)

NOTIFY_PUSH_RETRY_SEC = int(os.getenv("NOTIFY_PUSH_RETRY_SEC", "600"))
MAX_DELIVERY_ATTEMPTS = 5
RETRY_BATCH_LIMIT = 100


def run_push_retry() -> None:
    with SessionLocal() as session:
        now = datetime.utcnow()
        delivery_repository = NotificationDeliveryRepository(session)
        subscription_repository = PushSubscriptionRepository(session)
        preference_repository = NotificationPreferenceRepository(session)

        deliveries = (
            session.query(NotificationDelivery)
            .filter(
                NotificationDelivery.channel == DeliveryChannel.PUSH,
                NotificationDelivery.status == DeliveryStatus.FAILED,
                NotificationDelivery.attempts < MAX_DELIVERY_ATTEMPTS,
            )
            .order_by(NotificationDelivery.last_attempt_at.asc())
            .limit(RETRY_BATCH_LIMIT)
            .all()
        )

        retried = succeeded = 0
        for delivery in deliveries:
            if delivery.last_attempt_at is not None:
                backoff = timedelta(
                    seconds=max(delivery.attempts or 0, 1) * NOTIFY_PUSH_RETRY_SEC
                )
                if now - delivery.last_attempt_at < backoff:
                    continue  # not due yet — back off harder after each failure

            notification = (
                session.query(Notification)
                .filter(Notification.id == delivery.notification_id)
                .first()
            )
            if notification is None or notification.deleted_at is not None:
                delivery_repository.mark_attempt(
                    delivery, DeliveryStatus.FAILED, error="notification no longer exists"
                )
                continue

            user = session.query(User).filter(User.id == notification.user_id).first()
            if user is None:
                delivery_repository.mark_attempt(
                    delivery, DeliveryStatus.FAILED, error="recipient no longer exists"
                )
                continue

            preferences = preference_repository.get_by_user(user.id)
            if preferences is not None and not preferences.push_enabled:
                delivery_repository.mark_attempt(
                    delivery, DeliveryStatus.FAILED, error="push disabled in preferences"
                )
                continue

            if not subscription_repository.list_active_by_user(user.id):
                delivery_repository.mark_attempt(
                    delivery, DeliveryStatus.FAILED, error="no active push subscriptions"
                )
                continue

            retried += 1
            success = PushChannel(session).send(notification, user)
            delivery_repository.mark_attempt(
                delivery,
                DeliveryStatus.SENT if success else DeliveryStatus.FAILED,
                error=None if success else "push retry failed",
            )
            if success:
                succeeded += 1

        if deliveries:
            logger.info(
                "Push retry: %d candidate(s), %d retried, %d succeeded",
                len(deliveries), retried, succeeded,
            )
