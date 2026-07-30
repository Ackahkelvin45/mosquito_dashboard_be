from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.notification.enums import (
    DeliveryChannel,
    DeliveryStatus,
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
    PushProvider,
)


class Notification(Base):
    """One row per recipient (fan-out on write): reads are always user_id = me."""
    __tablename__ = "notifications"
    __table_args__ = (
        # Composite indexes for the hot read paths: the list (per-user, newest
        # first), the unread badge, and the dedupe window lookup.
        Index("ix_notifications_user_id_created_at", "user_id", "created_at"),
        Index("ix_notifications_user_id_read_at", "user_id", "read_at"),
        Index("ix_notifications_dedupe_key_created_at", "dedupe_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    cluster_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("device_clusters.id"), nullable=True, index=True)
    device_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("devices.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(1000))
    notification_type: Mapped[NotificationType] = mapped_column(Enum(NotificationType))
    severity: Mapped[NotificationSeverity] = mapped_column(
        Enum(NotificationSeverity), default=NotificationSeverity.INFO
    )
    category: Mapped[NotificationCategory] = mapped_column(
        Enum(NotificationCategory), default=NotificationCategory.SYSTEM
    )
    # Structured context for the FE (species, voltage, uuid, ...).
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Lucide icon name hint for the FE.
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # FE route, e.g. "/devices/42".
    action_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Throttling key, e.g. "low_battery:device:42".
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Soft delete marker — rows are purged by the cleanup job later.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Rows with scheduled_for > now are hidden until due.
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Cleanup job purges after expiry.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Naive UTC — house convention on the event path.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return (
            f"Notification(id={self.id}, user_id={self.user_id}, "
            f"type={self.notification_type}, severity={self.severity})"
        )


class PushSubscription(Base):
    """One row per browser/device a user enabled push on."""
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    p256dh: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[PushProvider] = mapped_column(Enum(PushProvider), default=PushProvider.WEBPUSH)
    browser: Mapped[str | None] = mapped_column(String(50), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"PushSubscription(id={self.id}, user_id={self.user_id}, active={self.active})"


class NotificationPreference(Base):
    """Per-user notification toggles, lazily created with defaults on first read."""
    __tablename__ = "notification_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), unique=True, index=True)
    species_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    battery_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    offline_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    admin_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    researcher_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    in_app_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"NotificationPreference(id={self.id}, user_id={self.user_id})"


class NotificationDelivery(Base):
    """Per-(notification, channel) delivery attempt tracking — powers retries."""
    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    notification_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notifications.id"), nullable=False, index=True
    )
    channel: Mapped[DeliveryChannel] = mapped_column(Enum(DeliveryChannel))
    status: Mapped[DeliveryStatus] = mapped_column(Enum(DeliveryStatus), default=DeliveryStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return (
            f"NotificationDelivery(id={self.id}, notification_id={self.notification_id}, "
            f"channel={self.channel}, status={self.status})"
        )
