"""Audit log (FR-27): who did what, when — login activity, configuration
changes, alert-setting changes. A forensic table: rows are written once and
never updated; actor_email is a deliberate denormalised snapshot so the row
still tells its story if the user row later changes or disappears.
"""
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuditAction(StrEnum):
    """Typo-proof action names (mirrors the NotificationEvent StrEnum pattern
    in app/notification/events.py). A new call site adds a value here first."""
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILED = "LOGIN_FAILED"
    TWO_FACTOR_SENT = "TWO_FACTOR_SENT"
    TWO_FACTOR_SUCCESS = "TWO_FACTOR_SUCCESS"
    TWO_FACTOR_FAILED = "TWO_FACTOR_FAILED"
    TWO_FACTOR_ENABLED = "TWO_FACTOR_ENABLED"
    TWO_FACTOR_DISABLED = "TWO_FACTOR_DISABLED"
    PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED"
    PASSWORD_RESET_COMPLETED = "PASSWORD_RESET_COMPLETED"
    PASSWORD_RESET_CLI = "PASSWORD_RESET_CLI"
    USER_CREATED = "USER_CREATED"
    USER_UPDATED = "USER_UPDATED"
    DEVICE_CREATED = "DEVICE_CREATED"
    DEVICE_UPDATED = "DEVICE_UPDATED"
    DEVICE_DELETED = "DEVICE_DELETED"
    CLUSTER_CREATED = "CLUSTER_CREATED"
    CLUSTER_UPDATED = "CLUSTER_UPDATED"
    CLUSTER_DELETED = "CLUSTER_DELETED"
    API_KEY_CREATED = "API_KEY_CREATED"
    API_KEY_REVOKED = "API_KEY_REVOKED"
    API_KEYS_BULK_REVOKED = "API_KEYS_BULK_REVOKED"
    MOSQUITO_EVENT_DELETED = "MOSQUITO_EVENT_DELETED"
    SIGHTING_DISMISSED = "SIGHTING_DISMISSED"
    RESEARCHER_REQUEST_DECIDED = "RESEARCHER_REQUEST_DECIDED"
    ALERT_SETTINGS_UPDATED = "ALERT_SETTINGS_UPDATED"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    # Nullable: failed logins and CLI actions have no authenticated actor row.
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    actor_email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    action: Mapped[str] = mapped_column(String(50), index=True)
    target_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Small structured context (changed fields, request email, …). NEVER put
    # secrets here: no passwords, OTP codes, raw API keys or tokens.
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # request.client.host — behind the deploy proxy this is the proxy address
    # until proxy-headers are configured; still stored because it is at least
    # not attacker-controlled (unlike X-Forwarded-For without a trust list).
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self):
        return f"AuditLog(action={self.action}, actor={self.actor_email}, at={self.occurred_at})"
