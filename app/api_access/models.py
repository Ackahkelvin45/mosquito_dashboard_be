import hashlib
import secrets
from datetime import datetime

from sqlalchemy import Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# Every key starts with this so a leaked one is recognisable in logs/scans
# (same idea as GitHub's ghp_ / Stripe's sk_ prefixes).
KEY_PREFIX = "mk_"


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # First characters of the raw key — display only, never enough to use.
    key_prefix: Mapped[str] = mapped_column(String(12))
    # SHA-256 hex of the full raw key. The raw key is 256-bit random, so a fast
    # unsalted hash is safe here (offline cracking is infeasible) and, unlike
    # bcrypt, allows a single indexed exact-match lookup per request.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    # utcnow, not now(): expiry math must not depend on the host timezone.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Soft revoke keeps the row (and its usage stats) as an audit trail.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Who revoked it — the owner themselves, or a super admin.
    revoked_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_requests: Mapped[int] = mapped_column(Integer, default=0)

    # Two FKs to users — both relationships must name theirs explicitly.
    user = relationship("User", foreign_keys=[user_id])
    revoked_by_user = relationship("User", foreign_keys=[revoked_by])

    @staticmethod
    def generate() -> tuple[str, str, str]:
        """Mint a fresh key → (raw_key, display_prefix, sha256_hash).

        The raw key is returned to the caller exactly once and never stored.
        """
        raw = KEY_PREFIX + secrets.token_urlsafe(32)
        return raw, raw[:12], ApiKey.hash_key(raw)

    @staticmethod
    def hash_key(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.utcnow()

    def __repr__(self):
        return f"ApiKey(id={self.id}, prefix={self.key_prefix}, user_id={self.user_id})"
