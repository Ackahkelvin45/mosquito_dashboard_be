"""Small persistent app-wide switches (currently just the global 2FA gate).

Same shape as app/notification/alert_settings.py: rows override defaults, an
in-process TTL cache keeps the login hot path off the DB, and the PUT
endpoint invalidates for read-your-writes. Single-process deployment.
"""
import logging
import time as _time
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, SessionLocal

logger = logging.getLogger(__name__)

_TTL_SECONDS = 30.0

TWO_FACTOR_LOGIN_ENABLED = "two_factor_login_enabled"
# Absent row = the default: 2FA active app-wide (per-user flags then decide).
_DEFAULTS: dict[str, bool] = {TWO_FACTOR_LOGIN_ENABLED: True}


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    value: Mapped[str] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 onupdate=datetime.utcnow)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


_snapshot: dict[str, bool] | None = None
_snapshot_at: float = 0.0


def _load(session) -> dict[str, bool]:
    values = dict(_DEFAULTS)
    for row in session.query(AppSetting).all():
        if row.name in values:
            values[row.name] = str(row.value).strip().lower() in ("1", "true", "yes", "on")
    return values


def get_app_settings(session=None) -> dict[str, bool]:
    """Never raises — serves the previous snapshot (or defaults) on failure."""
    global _snapshot, _snapshot_at
    now = _time.monotonic()
    if _snapshot is not None and (now - _snapshot_at) < _TTL_SECONDS:
        return _snapshot
    try:
        if session is not None:
            fresh = _load(session)
        else:
            with SessionLocal() as own:
                fresh = _load(own)
    except Exception:
        logger.exception("Could not refresh app settings — serving previous values")
        fresh = _snapshot if _snapshot is not None else dict(_DEFAULTS)
    _snapshot = fresh
    _snapshot_at = now
    return fresh


def two_factor_login_enabled(session=None) -> bool:
    """The master 2FA gate: when False, NOBODY gets a login challenge — the
    per-user flags are kept but dormant until an admin turns this back on."""
    return get_app_settings(session)[TWO_FACTOR_LOGIN_ENABLED]


def set_app_setting(session, name: str, value: bool, updated_by: int | None) -> None:
    if name not in _DEFAULTS:
        raise ValueError(f"Unknown app setting '{name}'")
    row = session.query(AppSetting).filter(AppSetting.name == name).first()
    if row is None:
        session.add(AppSetting(name=name, value="true" if value else "false",
                               updated_by=updated_by))
    else:
        row.value = "true" if value else "false"
        row.updated_by = updated_by
    session.commit()
    invalidate()


def invalidate() -> None:
    global _snapshot, _snapshot_at
    _snapshot = None
    _snapshot_at = 0.0
