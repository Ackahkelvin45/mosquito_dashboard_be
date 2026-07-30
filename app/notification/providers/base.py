"""Push provider abstraction — one implementation per push transport.

`webpush` is implemented; `fcm`/`apns` are registered stubs. Providers are
selected per subscription via `providers/registry.get_provider`.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from app.notification.models import PushSubscription


@dataclass
class PushResult:
    """Outcome of one push attempt against one subscription."""
    success: bool
    # True when the push service says the subscription is gone (404/410) and
    # the row should be deactivated immediately rather than retried.
    should_deactivate: bool = False
    error: Optional[str] = None


class PushProviderBase(ABC):
    @abstractmethod
    def send(self, subscription: PushSubscription, payload: dict) -> PushResult:
        """Deliver `payload` (JSON-serialisable dict) to `subscription`."""
        raise NotImplementedError
