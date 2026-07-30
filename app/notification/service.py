"""NotificationService — THE single entry point for creating and managing
notifications.

Business logic never constructs Notification rows directly: it goes through
`app/notification/events.emit(...)` (preferred) or the imperative methods
here. Fan-out happens on write — one row per recipient — so reads are always
`user_id = me` with zero visibility joins.

Channel dispatch (push/email) runs on a daemon thread with its own
SessionLocal (house pattern from device_location_service._dispatch_geocode)
so request/MQTT paths are never blocked by network calls.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import List, Optional, Sequence

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.authentication.enums import UserRole
from app.authentication.models import User
from app.core.database import SessionLocal
from app.core.pagination import Page
from app.notification.channels.email import EmailChannel
from app.notification.channels.push import PushChannel
from app.notification.enums import (
    DeliveryChannel,
    DeliveryStatus,
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
)
from app.notification.models import Notification
from app.notification.repository.notification_repository import (
    NotificationDeliveryRepository,
    NotificationPreferenceRepository,
    NotificationRepository,
    PushSubscriptionRepository,
)
from app.notification.schema import (
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
    NotificationResponse,
    PushSubscriptionCreate,
    PushSubscriptionResponse,
    ReadAllResponse,
    UnreadCountResponse,
    VapidPublicKeyResponse,
)

logger = logging.getLogger(__name__)

# Preference toggle -> the notification types it suppresses. in_app_enabled is
# the master switch (checked separately); push/email gate dispatch only.
PREFERENCE_GATES: dict[str, set[NotificationType]] = {
    "species_alerts": {
        NotificationType.SPECIES_DETECTED,
        NotificationType.ACTIVITY_SURGE,
    },
    "battery_alerts": {NotificationType.LOW_BATTERY},
    "offline_alerts": {
        NotificationType.DEVICE_OFFLINE,
        NotificationType.DEVICE_ONLINE,
    },
    "admin_alerts": {
        NotificationType.USER_REGISTERED,
        NotificationType.UNKNOWN_DEVICE,
        NotificationType.INVALID_PAYLOAD,
    },
    "researcher_alerts": {
        NotificationType.RESEARCHER_REQUEST_SUBMITTED,
        NotificationType.RESEARCHER_REQUEST_APPROVED,
        NotificationType.RESEARCHER_REQUEST_REJECTED,
    },
}

# Email is wired but deliberately quiet: it only auto-sends for summaries and
# CRITICAL alerts, and only when the user opted in via email_enabled.
EMAIL_AUTO_TYPES = {NotificationType.DAILY_SUMMARY, NotificationType.WEEKLY_SUMMARY}

# Stand-in for users with no notification_preferences row yet — mirrors the
# model/migration defaults (all True except email). Used by the bulk fan-out
# path so it never has to INSERT preference rows on the MQTT hot path.
_DEFAULT_PREFERENCES = SimpleNamespace(
    species_alerts=True,
    battery_alerts=True,
    offline_alerts=True,
    admin_alerts=True,
    researcher_alerts=True,
    email_enabled=False,
    push_enabled=True,
    in_app_enabled=True,
)


class NotificationService:
    def __init__(self, session: Session):
        self.session = session
        self.notification_repository = NotificationRepository(session)
        self.push_subscription_repository = PushSubscriptionRepository(session)
        self.preference_repository = NotificationPreferenceRepository(session)
        self.delivery_repository = NotificationDeliveryRepository(session)

    # ── Creation / fan-out ───────────────────────────────────────────────────

    def send(
        self,
        user_id: int,
        *,
        title: str,
        body: str,
        notification_type: NotificationType,
        severity: NotificationSeverity,
        category: NotificationCategory,
        payload: Optional[dict] = None,
        icon: Optional[str] = None,
        action_url: Optional[str] = None,
        cluster_id: Optional[int] = None,
        device_id: Optional[int] = None,
        dedupe_key: Optional[str] = None,
        dedupe_window_minutes: Optional[int] = None,
        expires_at: Optional[datetime] = None,
        scheduled_for: Optional[datetime] = None,
        channels: bool = True,
    ) -> Optional[NotificationResponse]:
        """Create one notification for one user, respecting dedupe + prefs.

        Returns None when the notification was suppressed (dedupe window hit,
        in-app disabled, or the type is gated off by a preference toggle).
        `channels=False` creates the in-app row only, with no push/email.
        """
        if dedupe_key and dedupe_window_minutes:
            since = datetime.utcnow() - timedelta(minutes=dedupe_window_minutes)
            if self.notification_repository.find_recent_by_dedupe_key(dedupe_key, since):
                return None

        preferences = self.preference_repository.get_or_create(user_id)
        if not self._allowed_by_preferences(preferences, notification_type):
            return None

        notification = self.notification_repository.create(
            user_id=user_id,
            title=title[:200],
            body=body[:1000],
            notification_type=notification_type,
            severity=severity,
            category=category,
            payload=payload,
            icon=icon,
            action_url=action_url,
            cluster_id=cluster_id,
            device_id=device_id,
            dedupe_key=dedupe_key,
            expires_at=expires_at,
            scheduled_for=scheduled_for,
        )
        self.send_in_app(notification)

        if channels and not self._is_scheduled_future(notification):
            push = bool(preferences.push_enabled)
            email = bool(preferences.email_enabled) and (
                severity == NotificationSeverity.CRITICAL
                or notification_type in EMAIL_AUTO_TYPES
            )
            if push or email:
                _dispatch_channels([(notification.id, user_id, push, email)])

        return NotificationResponse.model_validate(notification)

    def send_bulk(
        self,
        user_ids: Sequence[int],
        *,
        title: str,
        body: str,
        notification_type: NotificationType,
        severity: NotificationSeverity,
        category: NotificationCategory,
        payload: Optional[dict] = None,
        icon: Optional[str] = None,
        action_url: Optional[str] = None,
        cluster_id: Optional[int] = None,
        device_id: Optional[int] = None,
        dedupe_key: Optional[str] = None,
        dedupe_window_minutes: Optional[int] = None,
        expires_at: Optional[datetime] = None,
        scheduled_for: Optional[datetime] = None,
        channels: bool = True,
    ) -> int:
        """Fan one event out to many recipients: single dedupe check, per-user
        preference filtering, one bulk INSERT/commit. Returns rows created."""
        if not user_ids:
            return 0
        if dedupe_key and dedupe_window_minutes:
            since = datetime.utcnow() - timedelta(minutes=dedupe_window_minutes)
            if self.notification_repository.find_recent_by_dedupe_key(dedupe_key, since):
                return 0

        now = datetime.utcnow()
        deliver_now = not (scheduled_for and scheduled_for > now)
        unique_user_ids = list(dict.fromkeys(user_ids))  # de-dup, keep order
        # One query for every recipient's preferences; users without a row get
        # the model defaults in Python (no per-user SELECT/INSERT in the loop).
        preferences_by_user = self.preference_repository.get_by_users(unique_user_ids)
        rows: List[Notification] = []
        channel_flags: List[tuple[int, bool, bool]] = []  # (user_id, push, email)
        for user_id in unique_user_ids:
            preferences = preferences_by_user.get(user_id, _DEFAULT_PREFERENCES)
            if not self._allowed_by_preferences(preferences, notification_type):
                continue
            rows.append(
                Notification(
                    user_id=user_id,
                    title=title[:200],
                    body=body[:1000],
                    notification_type=notification_type,
                    severity=severity,
                    category=category,
                    payload=payload,
                    icon=icon,
                    action_url=action_url,
                    cluster_id=cluster_id,
                    device_id=device_id,
                    dedupe_key=dedupe_key,
                    expires_at=expires_at,
                    scheduled_for=scheduled_for,
                    delivered_at=now if deliver_now else None,
                )
            )
            push = bool(preferences.push_enabled)
            email = bool(preferences.email_enabled) and (
                severity == NotificationSeverity.CRITICAL
                or notification_type in EMAIL_AUTO_TYPES
            )
            channel_flags.append((user_id, push, email))

        # create_bulk returns the new ids captured before commit — reading
        # row.id here would trigger one expire_on_commit refresh SELECT per row.
        row_ids = self.notification_repository.create_bulk(rows)

        if channels and deliver_now:
            entries = [
                (notification_id, user_id, push, email)
                for notification_id, (user_id, push, email) in zip(row_ids, channel_flags)
                if push or email
            ]
            if entries:
                _dispatch_channels(entries)

        return len(rows)

    # ── Recipient resolution (RBAC fan-out per plan §3) ─────────────────────

    def notify_user(self, user_id: int, **kwargs) -> Optional[NotificationResponse]:
        """Alias for send() — reads better at call sites about a person."""
        return self.send(user_id, **kwargs)

    def notify_cluster(self, cluster_id: Optional[int], **kwargs) -> int:
        """Cluster-scoped fan-out per plan §3: members (users.cluster_id ==
        cluster_id) + all SUPER_ADMINs. `cluster_id=None` (device without
        cluster) targets SUPER_ADMINs only. Public clusters do NOT broadcast
        to everyone.

        Deliberately does NOT consult the legacy `cluster_admins` M2M table:
        RBAC_PLAN deprecated it ("unused for auth") and its rows may reference
        users whose `cluster_id` points elsewhere — including them would leak
        another cluster's device alerts to cross-cluster users. Admins who
        legitimately belong to the cluster are already covered by the
        `users.cluster_id` membership filter.
        """
        recipient_ids = self._super_admin_ids()
        if cluster_id is not None:
            member_rows = (
                self.session.query(User.id)
                .filter(User.cluster_id == cluster_id, User.is_active.is_(True))
                .all()
            )
            recipient_ids.update(user_id for (user_id,) in member_rows)
        kwargs.setdefault("cluster_id", cluster_id)
        return self.send_bulk(sorted(recipient_ids), **kwargs)

    def notify_admins(self, **kwargs) -> int:
        """Fan out to every active SUPER_ADMIN (approval rights are theirs)."""
        return self.send_bulk(sorted(self._super_admin_ids()), **kwargs)

    def broadcast(self, **kwargs) -> int:
        """Fan out to every active user."""
        rows = self.session.query(User.id).filter(User.is_active.is_(True)).all()
        return self.send_bulk([user_id for (user_id,) in rows], **kwargs)

    def _super_admin_ids(self) -> set[int]:
        rows = (
            self.session.query(User.id)
            .filter(User.role == UserRole.SUPER_ADMIN, User.is_active.is_(True))
            .all()
        )
        return {user_id for (user_id,) in rows}

    # ── Per-notification actions (ownership-checked) ─────────────────────────

    def _get_owned_or_404(self, notification_id: int, user_id: int) -> Notification:
        # 404 (not 403) — never reveal that another user's notification exists.
        notification = self.notification_repository.get_owned(notification_id, user_id)
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")
        return notification

    def mark_read(self, notification_id: int, user_id: int) -> NotificationResponse:
        notification = self._get_owned_or_404(notification_id, user_id)
        self.notification_repository.mark_read(notification)
        return NotificationResponse.model_validate(notification)

    def mark_unread(self, notification_id: int, user_id: int) -> NotificationResponse:
        notification = self._get_owned_or_404(notification_id, user_id)
        self.notification_repository.mark_unread(notification)
        return NotificationResponse.model_validate(notification)

    def mark_all_read(self, user_id: int) -> ReadAllResponse:
        updated = self.notification_repository.mark_all_read(user_id)
        return ReadAllResponse(updated=updated)

    def archive(self, notification_id: int, user_id: int) -> NotificationResponse:
        notification = self._get_owned_or_404(notification_id, user_id)
        self.notification_repository.archive(notification)
        return NotificationResponse.model_validate(notification)

    def unarchive(self, notification_id: int, user_id: int) -> NotificationResponse:
        notification = self._get_owned_or_404(notification_id, user_id)
        self.notification_repository.unarchive(notification)
        return NotificationResponse.model_validate(notification)

    def delete(self, notification_id: int, user_id: int) -> None:
        notification = self._get_owned_or_404(notification_id, user_id)
        self.notification_repository.soft_delete(notification)

    def unread_count(self, user_id: int) -> UnreadCountResponse:
        return UnreadCountResponse(count=self.notification_repository.unread_count(user_id))

    def list_notifications(
        self,
        user_id: int,
        *,
        page: int = 1,
        page_size: int = 20,
        unread_only: bool = False,
        category: Optional[NotificationCategory] = None,
        severity: Optional[NotificationSeverity] = None,
        notification_type: Optional[NotificationType] = None,
        archived: bool = False,
        search: Optional[str] = None,
        sort: str = "newest",
    ) -> Page[NotificationResponse]:
        # Filtering/COUNT/OFFSET happen in SQL (high-volume table) — the repo
        # returns (items, total) and we build the house Page envelope here.
        items, total = self.notification_repository.list_page(
            user_id,
            page=page,
            page_size=page_size,
            unread_only=unread_only,
            category=category,
            severity=severity,
            notification_type=notification_type,
            archived=archived,
            search=search,
            sort=sort,
        )
        total_pages = (total + page_size - 1) // page_size if page_size else 0
        return Page[NotificationResponse](
            items=[NotificationResponse.model_validate(n) for n in items],
            total=total, page=page, page_size=page_size, total_pages=total_pages,
        )

    # ── Preferences ──────────────────────────────────────────────────────────

    def get_preferences(self, user_id: int) -> NotificationPreferenceResponse:
        preference = self.preference_repository.get_or_create(user_id)
        return NotificationPreferenceResponse.model_validate(preference)

    def update_preferences(
        self, user_id: int, data: NotificationPreferenceUpdate
    ) -> NotificationPreferenceResponse:
        preference = self.preference_repository.update_preferences(
            user_id, **data.model_dump(exclude_unset=True)
        )
        return NotificationPreferenceResponse.model_validate(preference)

    @staticmethod
    def _allowed_by_preferences(preferences, notification_type: NotificationType) -> bool:
        # in_app disabled = no row at all (simpler and safer than hiding).
        if not preferences.in_app_enabled:
            return False
        for toggle, gated_types in PREFERENCE_GATES.items():
            if notification_type in gated_types and not getattr(preferences, toggle):
                return False
        return True

    # ── Channels ─────────────────────────────────────────────────────────────

    @staticmethod
    def _is_scheduled_future(notification: Notification) -> bool:
        return bool(
            notification.scheduled_for
            and notification.scheduled_for > datetime.utcnow()
        )

    def send_in_app(self, notification: Notification) -> None:
        """In-app 'delivery' is the row itself — just stamp delivered_at
        (unless the row is scheduled for the future)."""
        if notification.delivered_at is None and not self._is_scheduled_future(notification):
            notification.delivered_at = datetime.utcnow()
            self.session.commit()

    def send_push(self, notification: Notification, user_id: int) -> bool:
        """Push-deliver one notification: creates the PUSH delivery row, fans
        out to the user's active subscriptions (provider per subscription),
        and records the outcome. Subscription failure counts/deactivation are
        handled inside the push channel."""
        user = self.session.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        if not self.push_subscription_repository.list_active_by_user(user_id):
            return False  # nothing to deliver to — no delivery row, no retry
        delivery = self.delivery_repository.create(notification.id, DeliveryChannel.PUSH)
        success = PushChannel(self.session).send(notification, user)
        self.delivery_repository.mark_attempt(
            delivery,
            DeliveryStatus.SENT if success else DeliveryStatus.FAILED,
            error=None if success else "all push attempts failed",
        )
        return success

    def send_email(self, notification: Notification, user_id: int) -> bool:
        """Email-deliver one notification (explicit calls / summaries /
        critical alerts only — never automatic for every notification)."""
        user = self.session.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        delivery = self.delivery_repository.create(notification.id, DeliveryChannel.EMAIL)
        success = EmailChannel(self.session).send(notification, user)
        self.delivery_repository.mark_attempt(
            delivery,
            DeliveryStatus.SENT if success else DeliveryStatus.FAILED,
            error=None if success else "email send failed",
        )
        return success

    def send_test(self, user_id: int) -> NotificationResponse:
        """Create a TEST notification for the caller (super-admin route)."""
        response = self.send(
            user_id,
            title="Test notification",
            body="This is a test notification from the Mosquito Dashboard. "
                 "If you can read this, in-app notifications are working.",
            notification_type=NotificationType.TEST,
            severity=NotificationSeverity.INFO,
            category=NotificationCategory.SYSTEM,
            icon="bell",
            action_url="/notifications",
        )
        if response is None:
            raise HTTPException(
                status_code=400,
                detail="Test notification was suppressed — in-app notifications are disabled in your preferences",
            )
        return response

    # ── Maintenance ──────────────────────────────────────────────────────────

    def delete_expired(self) -> int:
        """Purge expired rows and soft-deleted rows older than 30 days.
        Returns total rows removed (cleanup job entry point)."""
        expired = self.notification_repository.delete_expired()
        purged = self.notification_repository.purge_soft_deleted(older_than_days=30)
        if expired or purged:
            logger.info("Notification cleanup: %s expired, %s purged", expired, purged)
        return expired + purged

    # ── Push subscriptions (routes call these) ───────────────────────────────

    def get_vapid_public_key(self) -> VapidPublicKeyResponse:
        # Imported here so the env read stays in one place (providers/webpush).
        from app.notification.providers.webpush import VAPID_PUBLIC_KEY
        return VapidPublicKeyResponse(publicKey=VAPID_PUBLIC_KEY)

    def subscribe_push(
        self, user_id: int, data: PushSubscriptionCreate
    ) -> PushSubscriptionResponse:
        subscription = self.push_subscription_repository.upsert(
            user_id,
            endpoint=data.endpoint,
            p256dh=data.keys.p256dh,
            auth=data.keys.auth,
            provider=data.provider,
            browser=data.browser,
            platform=data.platform,
            device_name=data.device_name,
        )
        return PushSubscriptionResponse.model_validate(subscription)

    def list_push_subscriptions(self, user_id: int) -> List[PushSubscriptionResponse]:
        subscriptions = self.push_subscription_repository.list_by_user(user_id)
        return [PushSubscriptionResponse.model_validate(s) for s in subscriptions]

    def unsubscribe_push(self, user_id: int, endpoint: str) -> None:
        """Idempotent — deleting an unknown endpoint is not an error."""
        self.push_subscription_repository.delete_by_endpoint(endpoint, user_id)


# ── Off-request channel dispatch (bounded worker pool, own session) ──────────
# Evolution of the house daemon-thread pattern from
# device_location_service._dispatch_geocode: never block the caller, never
# raise, always use a fresh SessionLocal. A shared, bounded executor instead of
# one thread per call — a burst of notifications queues work rather than
# spawning an unbounded number of threads.

_DISPATCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="notify-dispatch"
)


def _dispatch_channels(entries: List[tuple[int, int, bool, bool]]) -> None:
    """entries: (notification_id, user_id, push, email) per recipient."""
    try:
        _DISPATCH_EXECUTOR.submit(_deliver_channels, entries)
    except RuntimeError:  # interpreter shutting down — drop, never raise
        logger.warning("Dispatch executor unavailable; dropped %d entries", len(entries))


def _deliver_channels(entries: List[tuple[int, int, bool, bool]]) -> None:
    try:
        with SessionLocal() as session:
            service = NotificationService(session)
            for notification_id, user_id, push, email in entries:
                try:
                    notification = (
                        session.query(Notification)
                        .filter(Notification.id == notification_id)
                        .first()
                    )
                    if notification is None:
                        continue
                    if push:
                        service.send_push(notification, user_id)
                    if email:
                        service.send_email(notification, user_id)
                except Exception:
                    logger.exception(
                        "Channel delivery failed for notification %s", notification_id
                    )
    except Exception:  # never let background delivery take anything down
        logger.exception("Notification channel dispatch failed")
