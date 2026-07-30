"""Web Push (VAPID) provider backed by pywebpush.

All VAPID config comes from optional env vars — when unset, push is silently
disabled (the registry logs that once) and the app still starts fine.
"""
import json
import logging
import os

from app.notification.models import PushSubscription
from app.notification.providers.base import PushProviderBase, PushResult

logger = logging.getLogger(__name__)

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY") or None
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY") or None
VAPID_CLAIMS_EMAIL = os.getenv("VAPID_CLAIMS_EMAIL") or None

# Import lazily-guarded so a missing/broken dependency can never take the app
# down — the provider just reports failure instead.
try:
    from pywebpush import WebPushException, webpush
except Exception:  # pragma: no cover - depends on the environment
    webpush = None
    WebPushException = None


def is_webpush_configured() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_CLAIMS_EMAIL and webpush)


class WebPushProvider(PushProviderBase):
    def send(self, subscription: PushSubscription, payload: dict) -> PushResult:
        if webpush is None:
            return PushResult(success=False, error="pywebpush is not installed")
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{VAPID_CLAIMS_EMAIL}"},
            )
            return PushResult(success=True)
        except Exception as exc:
            if WebPushException is not None and isinstance(exc, WebPushException):
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                # 404/410 = the push service says this subscription is gone.
                return PushResult(
                    success=False,
                    should_deactivate=status_code in (404, 410),
                    error=str(exc)[:500],
                )
            return PushResult(success=False, error=str(exc)[:500])
