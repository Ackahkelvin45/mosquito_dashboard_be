"""Push channel — fans one notification out to every active subscription the
user has, via the provider matching each subscription."""
import logging

from app.authentication.models import User
from app.notification.channels.base import NotificationChannel
from app.notification.models import Notification
from app.notification.providers.registry import get_provider
from app.notification.repository.notification_repository import PushSubscriptionRepository

logger = logging.getLogger(__name__)


def build_push_payload(notification: Notification) -> dict:
    """The JSON body the service worker receives."""
    return {
        "notification_id": notification.id,
        "title": notification.title,
        "body": notification.body,
        "icon": notification.icon,
        "action_url": notification.action_url,
        "severity": str(notification.severity),
        "category": str(notification.category),
        "type": str(notification.notification_type),
        "payload": notification.payload,
    }


class PushChannel(NotificationChannel):
    def send(self, notification: Notification, user: User) -> bool:
        try:
            subscription_repository = PushSubscriptionRepository(self.session)
            subscriptions = subscription_repository.list_active_by_user(user.id)
            if not subscriptions:
                return False

            payload = build_push_payload(notification)
            any_success = False
            for subscription in subscriptions:
                provider = get_provider(subscription.provider)
                if provider is None:
                    continue  # unconfigured provider — registry already logged it
                result = provider.send(subscription, payload)
                if result.success:
                    any_success = True
                    subscription_repository.record_success(subscription)
                else:
                    if result.should_deactivate:
                        # The push service says this subscription is gone.
                        subscription_repository.deactivate(subscription)
                    else:
                        subscription_repository.record_failure(subscription)
                    logger.warning(
                        "Push delivery failed (notification=%s subscription=%s): %s",
                        notification.id, subscription.id, result.error,
                    )
            return any_success
        except Exception:
            logger.exception(
                "Push channel failed for notification %s user %s", notification.id, user.id
            )
            return False
