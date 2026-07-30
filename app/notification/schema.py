from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.notification.enums import (
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
    PushProvider,
)


class NotificationResponse(BaseModel):
    """A single notification as seen by its recipient (never exposes user_id)."""
    id: int = Field(..., description="ID of the notification")
    title: str = Field(..., description="Short headline of the notification")
    body: str = Field(..., description="Full message body of the notification")
    notification_type: NotificationType = Field(..., description="Fine-grained notification type")
    severity: NotificationSeverity = Field(..., description="Severity level (INFO/SUCCESS/WARNING/CRITICAL)")
    category: NotificationCategory = Field(..., description="High-level category for filtering and icons")
    payload: Optional[dict] = Field(None, description="Structured context (species, voltage, uuid, ...)")
    icon: Optional[str] = Field(None, description="Lucide icon name hint for the frontend")
    action_url: Optional[str] = Field(None, description="Frontend route to open when clicked, e.g. /devices/42")
    cluster_id: Optional[int] = Field(None, description="ID of the cluster this notification relates to")
    device_id: Optional[int] = Field(None, description="ID of the device this notification relates to")
    read_at: Optional[datetime] = Field(None, description="When the recipient read the notification (naive UTC)")
    archived_at: Optional[datetime] = Field(None, description="When the recipient archived the notification (naive UTC)")
    delivered_at: Optional[datetime] = Field(None, description="When the notification was delivered in-app (naive UTC)")
    created_at: datetime = Field(..., description="When the notification was created (naive UTC)")
    expires_at: Optional[datetime] = Field(None, description="When the notification expires and is purged (naive UTC)")

    model_config = ConfigDict(from_attributes=True)


class NotificationPreferenceResponse(BaseModel):
    """The caller's notification toggles."""
    species_alerts: bool = Field(..., description="Receive species detection and activity surge alerts")
    battery_alerts: bool = Field(..., description="Receive low battery alerts")
    offline_alerts: bool = Field(..., description="Receive device offline/online alerts")
    admin_alerts: bool = Field(..., description="Receive admin alerts (registrations, unknown devices, bad payloads)")
    researcher_alerts: bool = Field(..., description="Receive researcher request alerts")
    email_enabled: bool = Field(..., description="Allow email delivery (summaries and critical alerts)")
    push_enabled: bool = Field(..., description="Allow browser push delivery")
    in_app_enabled: bool = Field(..., description="Allow in-app notifications (master switch for creation)")

    model_config = ConfigDict(from_attributes=True)


class NotificationPreferenceUpdate(BaseModel):
    """Partial update — only the provided toggles change."""
    species_alerts: Optional[bool] = Field(None, description="Receive species detection and activity surge alerts")
    battery_alerts: Optional[bool] = Field(None, description="Receive low battery alerts")
    offline_alerts: Optional[bool] = Field(None, description="Receive device offline/online alerts")
    admin_alerts: Optional[bool] = Field(None, description="Receive admin alerts (registrations, unknown devices, bad payloads)")
    researcher_alerts: Optional[bool] = Field(None, description="Receive researcher request alerts")
    email_enabled: Optional[bool] = Field(None, description="Allow email delivery (summaries and critical alerts)")
    push_enabled: Optional[bool] = Field(None, description="Allow browser push delivery")
    in_app_enabled: Optional[bool] = Field(None, description="Allow in-app notifications (master switch for creation)")


class PushSubscriptionKeys(BaseModel):
    """Web Push encryption keys as produced by PushManager.subscribe()."""
    p256dh: str = Field(..., min_length=1, max_length=255, description="Client public key (p256dh)")
    auth: str = Field(..., min_length=1, max_length=255, description="Client auth secret")


class PushSubscriptionCreate(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=500, description="Push service endpoint URL (upsert key)")
    keys: PushSubscriptionKeys = Field(..., description="Web Push encryption keys")
    provider: PushProvider = Field(PushProvider.WEBPUSH, description="Push provider for this subscription")
    browser: Optional[str] = Field(None, max_length=50, description="Browser name, e.g. Chrome")
    platform: Optional[str] = Field(None, max_length=50, description="OS/platform, e.g. macOS")
    device_name: Optional[str] = Field(None, max_length=100, description="Human-friendly device label")


class PushSubscriptionResponse(BaseModel):
    id: int = Field(..., description="ID of the push subscription")
    endpoint: str = Field(..., description="Push service endpoint URL")
    provider: PushProvider = Field(..., description="Push provider for this subscription")
    browser: Optional[str] = Field(None, description="Browser name, e.g. Chrome")
    platform: Optional[str] = Field(None, description="OS/platform, e.g. macOS")
    device_name: Optional[str] = Field(None, description="Human-friendly device label")
    active: bool = Field(..., description="Whether the subscription is active (deactivated after repeated failures)")
    last_used_at: Optional[datetime] = Field(None, description="When a push was last delivered through it (naive UTC)")
    created_at: datetime = Field(..., description="When the subscription was registered (naive UTC)")

    model_config = ConfigDict(from_attributes=True)


class PushUnsubscribe(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=500, description="Push service endpoint URL to remove")


class VapidPublicKeyResponse(BaseModel):
    publicKey: Optional[str] = Field(None, description="VAPID public key; null when push is disabled server-side")


class UnreadCountResponse(BaseModel):
    count: int = Field(..., description="Number of unread, non-archived notifications for the caller")


class ReadAllResponse(BaseModel):
    updated: int = Field(..., description="Number of notifications marked as read")
