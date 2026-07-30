"""Maps a subscription's PushProvider enum to a provider instance.

Unconfigured/unimplemented providers resolve to None (logged once per
provider) so callers can simply skip them — push being disabled must never
raise or spam the logs.
"""
import logging
from typing import Optional

from app.notification.enums import PushProvider
from app.notification.providers.base import PushProviderBase
from app.notification.providers.webpush import WebPushProvider, is_webpush_configured

logger = logging.getLogger(__name__)

_webpush_instance: Optional[WebPushProvider] = None
_logged_unavailable: set[str] = set()


def _log_once(key: str, message: str) -> None:
    if key not in _logged_unavailable:
        _logged_unavailable.add(key)
        logger.info(message)


def get_provider(provider: PushProvider) -> Optional[PushProviderBase]:
    """Provider instance for `provider`, or None when it can't send."""
    if provider == PushProvider.WEBPUSH:
        if not is_webpush_configured():
            _log_once(
                "webpush",
                "Web push is disabled: VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY/"
                "VAPID_CLAIMS_EMAIL are not configured (or pywebpush is missing)",
            )
            return None
        global _webpush_instance
        if _webpush_instance is None:
            _webpush_instance = WebPushProvider()
        return _webpush_instance

    # FCM / APNS are registered slots without an active implementation.
    _log_once(
        provider.value,
        f"Push provider {provider.value} is registered but not configured — skipping",
    )
    return None
