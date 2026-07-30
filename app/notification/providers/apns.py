"""Apple Push Notification service provider — registered but NOT implemented.

Placeholder for iOS/Safari native push. To activate: add an APNs client
dependency (e.g. httpx + JWT token auth against api.push.apple.com), implement
`send()` using the subscription's endpoint as the device token, and wire the
configuration check into `providers/registry.get_provider` (return an APNs
instance instead of logging it as unconfigured).
"""
from app.notification.models import PushSubscription
from app.notification.providers.base import PushProviderBase, PushResult


class APNSProvider(PushProviderBase):
    """Stub — raises until APNs support is actually implemented."""

    def send(self, subscription: PushSubscription, payload: dict) -> PushResult:
        raise NotImplementedError(
            "APNs push is not implemented. The provider is registered as a slot "
            "for Apple device tokens; see the module docstring for what "
            "activating it requires."
        )
