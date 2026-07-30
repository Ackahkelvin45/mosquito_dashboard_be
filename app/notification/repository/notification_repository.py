from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app.crud.base import BaseRepository
from app.notification.enums import (
    DeliveryChannel,
    DeliveryStatus,
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
    PushProvider,
)
from app.notification.models import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
    PushSubscription,
)

# A subscription is deactivated after this many consecutive failures.
MAX_PUSH_FAILURES = 5


class NotificationRepository(BaseRepository[Notification]):
    model = Notification

    def create(self, **fields) -> Notification:
        notification = Notification(**fields)
        self.session.add(notification)
        self.session.commit()
        self.session.refresh(notification)
        return notification

    def create_bulk(self, notifications: List[Notification]) -> List[int]:
        """Insert many rows with a single flush+commit (fan-out on write).

        Returns the new primary keys, captured after flush but BEFORE commit:
        expire_on_commit would otherwise turn every later `row.id` access into
        its own refresh SELECT (an N+1 on the fan-out hot path).
        """
        if not notifications:
            return []
        self.session.add_all(notifications)
        self.session.flush()
        ids = [notification.id for notification in notifications]
        self.session.commit()
        return ids

    def _visible_query(self, user_id: int, now: Optional[datetime] = None):
        """Base filter every read path shares: mine, not deleted, not expired,
        and scheduled rows hidden until due."""
        now = now or datetime.utcnow()
        return self.session.query(Notification).filter(
            Notification.user_id == user_id,
            Notification.deleted_at.is_(None),
            or_(Notification.scheduled_for.is_(None), Notification.scheduled_for <= now),
            or_(Notification.expires_at.is_(None), Notification.expires_at > now),
        )

    def list_page(
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
    ) -> Tuple[List[Notification], int]:
        """Return (page_items, total) filtered and paged in SQL.

        Deviation from the house load-all-then-paginate() idiom, on purpose:
        notifications are a high-volume per-user table, so filtering, COUNT and
        OFFSET/LIMIT happen in the database. The caller builds the same Page[T]
        envelope from the returned tuple.
        """
        query = self._visible_query(user_id)

        if archived:
            query = query.filter(Notification.archived_at.isnot(None))
        else:
            query = query.filter(Notification.archived_at.is_(None))
        if unread_only:
            query = query.filter(Notification.read_at.is_(None))
        if category is not None:
            query = query.filter(Notification.category == category)
        if severity is not None:
            query = query.filter(Notification.severity == severity)
        if notification_type is not None:
            query = query.filter(Notification.notification_type == notification_type)
        if search:
            # Escape LIKE metacharacters so a search for "100%" or "_" matches
            # literally instead of acting as a wildcard. (The value itself is
            # always a bound parameter — this is not an injection concern.)
            term = (
                search.strip()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            like = f"%{term}%"
            query = query.filter(
                or_(
                    Notification.title.ilike(like, escape="\\"),
                    Notification.body.ilike(like, escape="\\"),
                )
            )

        total = query.count()

        if sort == "oldest":
            query = query.order_by(Notification.created_at.asc(), Notification.id.asc())
        else:
            query = query.order_by(Notification.created_at.desc(), Notification.id.desc())

        items = query.offset((page - 1) * page_size).limit(page_size).all()
        return items, total

    def get_owned(self, notification_id: int, user_id: int) -> Optional[Notification]:
        """The row only if it belongs to `user_id` and isn't soft-deleted."""
        return (
            self.session.query(Notification)
            .filter(
                Notification.id == notification_id,
                Notification.user_id == user_id,
                Notification.deleted_at.is_(None),
            )
            .first()
        )

    def mark_read(self, notification: Notification) -> Notification:
        if notification.read_at is None:
            notification.read_at = datetime.utcnow()
            self.session.commit()
            self.session.refresh(notification)
        return notification

    def mark_unread(self, notification: Notification) -> Notification:
        if notification.read_at is not None:
            notification.read_at = None
            self.session.commit()
            self.session.refresh(notification)
        return notification

    def mark_all_read(self, user_id: int) -> int:
        """Single UPDATE — no row loading."""
        now = datetime.utcnow()
        updated = (
            self.session.query(Notification)
            .filter(
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
                Notification.deleted_at.is_(None),
                or_(Notification.scheduled_for.is_(None), Notification.scheduled_for <= now),
            )
            .update({Notification.read_at: now}, synchronize_session=False)
        )
        self.session.commit()
        return updated

    def archive(self, notification: Notification) -> Notification:
        if notification.archived_at is None:
            notification.archived_at = datetime.utcnow()
            self.session.commit()
            self.session.refresh(notification)
        return notification

    def unarchive(self, notification: Notification) -> Notification:
        if notification.archived_at is not None:
            notification.archived_at = None
            self.session.commit()
            self.session.refresh(notification)
        return notification

    def soft_delete(self, notification: Notification) -> Notification:
        if notification.deleted_at is None:
            notification.deleted_at = datetime.utcnow()
            self.session.commit()
        return notification

    def unread_count(self, user_id: int) -> int:
        return (
            self._visible_query(user_id)
            .filter(
                Notification.read_at.is_(None),
                Notification.archived_at.is_(None),
            )
            .count()
        )

    def find_recent_by_dedupe_key(self, dedupe_key: str, since: datetime) -> Optional[Notification]:
        """Most recent row carrying `dedupe_key` created at/after `since`.

        Deliberately ignores read/archive/delete state — a user clearing a
        notification must not re-arm the throttle window.
        """
        return (
            self.session.query(Notification)
            .filter(
                Notification.dedupe_key == dedupe_key,
                Notification.created_at >= since,
            )
            .order_by(Notification.created_at.desc())
            .first()
        )

    def delete_expired(self) -> int:
        """Hard-delete rows past their expires_at (cleanup job)."""
        now = datetime.utcnow()
        deleted = (
            self.session.query(Notification)
            .filter(Notification.expires_at.isnot(None), Notification.expires_at < now)
            .delete(synchronize_session=False)
        )
        self.session.commit()
        return deleted

    def purge_soft_deleted(self, older_than_days: int = 30) -> int:
        """Hard-delete rows soft-deleted more than `older_than_days` ago."""
        cutoff = datetime.utcnow() - timedelta(days=older_than_days)
        deleted = (
            self.session.query(Notification)
            .filter(Notification.deleted_at.isnot(None), Notification.deleted_at < cutoff)
            .delete(synchronize_session=False)
        )
        self.session.commit()
        return deleted


class PushSubscriptionRepository(BaseRepository[PushSubscription]):
    model = PushSubscription

    def upsert(
        self,
        user_id: int,
        *,
        endpoint: str,
        p256dh: Optional[str] = None,
        auth: Optional[str] = None,
        provider: PushProvider = PushProvider.WEBPUSH,
        browser: Optional[str] = None,
        platform: Optional[str] = None,
        device_name: Optional[str] = None,
    ) -> PushSubscription:
        """Insert or refresh a subscription keyed by endpoint.

        Re-subscribing reactivates the row and resets its failure count; a
        browser re-registering under a different account moves the endpoint to
        the new user (an endpoint identifies one browser profile).
        """
        subscription = (
            self.session.query(PushSubscription)
            .filter(PushSubscription.endpoint == endpoint)
            .first()
        )
        if subscription:
            changing_owner = subscription.user_id != user_id
            subscription.user_id = user_id
            subscription.p256dh = p256dh
            subscription.auth = auth
            subscription.provider = provider
            if changing_owner:
                # Endpoint changed hands (same browser, different account):
                # never carry the previous owner's metadata over to the new one.
                subscription.browser = browser
                subscription.platform = platform
                subscription.device_name = device_name
                subscription.last_used_at = None
            else:
                if browser is not None:
                    subscription.browser = browser
                if platform is not None:
                    subscription.platform = platform
                if device_name is not None:
                    subscription.device_name = device_name
            subscription.active = True
            subscription.failure_count = 0
        else:
            subscription = PushSubscription(
                user_id=user_id,
                endpoint=endpoint,
                p256dh=p256dh,
                auth=auth,
                provider=provider,
                browser=browser,
                platform=platform,
                device_name=device_name,
            )
            self.session.add(subscription)
        self.session.commit()
        self.session.refresh(subscription)
        return subscription

    def list_by_user(self, user_id: int) -> List[PushSubscription]:
        return (
            self.session.query(PushSubscription)
            .filter(PushSubscription.user_id == user_id)
            .order_by(PushSubscription.created_at.desc())
            .all()
        )

    def list_active_by_user(self, user_id: int) -> List[PushSubscription]:
        return (
            self.session.query(PushSubscription)
            .filter(
                PushSubscription.user_id == user_id,
                PushSubscription.active.is_(True),
            )
            .all()
        )

    def deactivate(self, subscription: PushSubscription) -> PushSubscription:
        subscription.active = False
        self.session.commit()
        return subscription

    def delete_by_endpoint(self, endpoint: str, user_id: int) -> bool:
        """Delete the caller's subscription for `endpoint`. Idempotent."""
        deleted = (
            self.session.query(PushSubscription)
            .filter(
                PushSubscription.endpoint == endpoint,
                PushSubscription.user_id == user_id,
            )
            .delete(synchronize_session=False)
        )
        self.session.commit()
        return deleted > 0

    def record_success(self, subscription: PushSubscription) -> PushSubscription:
        subscription.failure_count = 0
        subscription.last_used_at = datetime.utcnow()
        self.session.commit()
        return subscription

    def record_failure(self, subscription: PushSubscription) -> PushSubscription:
        """Bump the failure count; deactivate after MAX_PUSH_FAILURES in a row."""
        subscription.failure_count = (subscription.failure_count or 0) + 1
        if subscription.failure_count >= MAX_PUSH_FAILURES:
            subscription.active = False
        self.session.commit()
        return subscription


class NotificationPreferenceRepository(BaseRepository[NotificationPreference]):
    model = NotificationPreference

    def get_by_user(self, user_id: int) -> Optional[NotificationPreference]:
        return (
            self.session.query(NotificationPreference)
            .filter(NotificationPreference.user_id == user_id)
            .first()
        )

    def get_by_users(self, user_ids: Sequence[int]) -> Dict[int, NotificationPreference]:
        """All existing preference rows for `user_ids` in one query, keyed by
        user_id. Missing users are simply absent — bulk callers apply defaults
        in Python instead of INSERTing rows on the fan-out hot path."""
        if not user_ids:
            return {}
        rows = (
            self.session.query(NotificationPreference)
            .filter(NotificationPreference.user_id.in_(list(user_ids)))
            .all()
        )
        return {row.user_id: row for row in rows}

    def get_or_create(self, user_id: int) -> NotificationPreference:
        """Lazily create the row with defaults (all True except email_enabled)."""
        preference = self.get_by_user(user_id)
        if preference:
            return preference
        preference = NotificationPreference(user_id=user_id)
        self.session.add(preference)
        try:
            self.session.commit()
        except IntegrityError:
            # Two concurrent callers raced on the user_id unique constraint.
            # Roll back (also clears the poisoned session) and use the row the
            # winner created.
            self.session.rollback()
            preference = self.get_by_user(user_id)
            if preference is None:  # pragma: no cover - FK failure, not a race
                raise
            return preference
        self.session.refresh(preference)
        return preference

    def update_preferences(self, user_id: int, **fields) -> NotificationPreference:
        preference = self.get_or_create(user_id)
        for key, value in fields.items():
            if value is not None:
                setattr(preference, key, value)
        self.session.commit()
        self.session.refresh(preference)
        return preference


class NotificationDeliveryRepository(BaseRepository[NotificationDelivery]):
    model = NotificationDelivery

    def create(
        self, notification_id: int, channel: DeliveryChannel
    ) -> NotificationDelivery:
        delivery = NotificationDelivery(
            notification_id=notification_id,
            channel=channel,
            status=DeliveryStatus.PENDING,
        )
        self.session.add(delivery)
        self.session.commit()
        self.session.refresh(delivery)
        return delivery

    def mark_attempt(
        self,
        delivery: NotificationDelivery,
        status: DeliveryStatus,
        error: Optional[str] = None,
    ) -> NotificationDelivery:
        delivery.attempts = (delivery.attempts or 0) + 1
        delivery.last_attempt_at = datetime.utcnow()
        delivery.status = status
        delivery.error = error[:500] if error else None
        self.session.commit()
        self.session.refresh(delivery)
        return delivery

    def list_failed(self, max_attempts: int = 5, limit: int = 100) -> List[NotificationDelivery]:
        """FAILED deliveries still under the attempt cap — for the retry job."""
        return (
            self.session.query(NotificationDelivery)
            .filter(
                NotificationDelivery.status == DeliveryStatus.FAILED,
                NotificationDelivery.attempts < max_attempts,
            )
            .order_by(NotificationDelivery.last_attempt_at.asc())
            .limit(limit)
            .all()
        )
