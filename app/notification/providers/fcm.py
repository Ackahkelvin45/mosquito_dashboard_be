"""Firebase Cloud Messaging provider — registered but NOT implemented.

Placeholder for native mobile push. To activate: add the firebase-admin
dependency, implement `send()` against the FCM HTTP v1 API using the
subscription's endpoint as the device registration token, and wire the
configuration check into `providers/registry.get_provider` (return an FCM
instance instead of logging it as unconfigured).
"""
from app.notification.models import PushSubscription
from app.notification.providers.base import PushProviderBase, PushResult


class FCMProvider(PushProviderBase):
    """Stub — raises until FCM support is actually implemented."""

    def send(self, subscription: PushSubscription, payload: dict) -> PushResult:
        raise NotImplementedError(
            "FCM push is not implemented. The provider is registered as a slot "
            "for native Android/web FCM tokens; see the module docstring for "
            "what activating it requires."
        )
