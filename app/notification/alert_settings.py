"""DB-backed alert thresholds (FR-18 Phase A).

The env-derived module constants in app/notification/events.py remain the
DEFAULTS (and what the tests read); a row in `alert_settings` overrides one
value at runtime, editable by a super admin without a redeploy.

Reads go through an in-process cached SNAPSHOT (immutable dataclass, module
name rebound atomically — safe for the dispatch thread pool / scheduler /
MQTT callback to read concurrently). The TTL is only a backstop for edits
made directly in the DB; the PUT endpoint calls invalidate() for
read-your-writes. Single-process deployment — no cross-process concerns.

get_thresholds() NEVER raises: on any refresh failure it serves the previous
snapshot (or the env defaults on cold start) — a threshold read must never
cost a notification, let alone a reading.

Do NOT from-import individual threshold values anywhere: a from-import
freezes the boot-time value (that bug existed in app/jobs/summaries.py).
"""
import logging
import time as _time
from dataclasses import dataclass, fields

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime

from app.core.database import Base, SessionLocal

logger = logging.getLogger(__name__)

_TTL_SECONDS = 30.0


class AlertSetting(Base):
    """One overridden threshold. Absent row = env default applies."""
    __tablename__ = "alert_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    value: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 onupdate=datetime.utcnow)
    # Provenance without a full audit dependency (the route also audits).
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


@dataclass(frozen=True)
class Thresholds:
    battery_critical_v: float
    battery_urgent_v: float
    battery_critical_pct: int
    battery_urgent_pct: int
    surge_threshold: int
    surge_window_min: int
    temp_min: float
    temp_max: float
    humidity_min: float
    humidity_max: float


_INT_FIELDS = {"battery_critical_pct", "battery_urgent_pct", "surge_threshold", "surge_window_min"}

# name -> (min, max) hard validation bounds for the PUT endpoint. An admin
# typo like battery_critical_v=45 would page on every healthy device forever.
EDITABLE_BOUNDS: dict[str, tuple[float, float]] = {
    "battery_critical_v": (0.5, 15.0),
    "battery_urgent_v": (0.5, 15.0),
    "battery_critical_pct": (1, 100),
    "battery_urgent_pct": (1, 100),
    "surge_threshold": (1, 100_000),
    "surge_window_min": (1, 1440),
    "temp_min": (-50.0, 60.0),
    "temp_max": (-50.0, 60.0),
    "humidity_min": (0.0, 100.0),
    "humidity_max": (0.0, 100.0),
}

_snapshot: Thresholds | None = None
_snapshot_at: float = 0.0


def _env_defaults() -> Thresholds:
    # Imported lazily: events.py imports this module, so a top-level import
    # here would be circular. The events globals are the single env-derived
    # source of defaults (and what the test suite reads).
    from app.notification import events as ev
    return Thresholds(
        battery_critical_v=ev.NOTIFY_BATTERY_CRITICAL_V,
        battery_urgent_v=ev.BATTERY_URGENT_V,
        battery_critical_pct=ev.NOTIFY_BATTERY_CRITICAL_PCT,
        battery_urgent_pct=ev.BATTERY_URGENT_PCT,
        surge_threshold=ev.NOTIFY_SURGE_THRESHOLD,
        surge_window_min=ev.NOTIFY_SURGE_WINDOW_MIN,
        temp_min=ev.NOTIFY_TEMP_MIN,
        temp_max=ev.NOTIFY_TEMP_MAX,
        humidity_min=ev.NOTIFY_HUMIDITY_MIN,
        humidity_max=ev.NOTIFY_HUMIDITY_MAX,
    )


def _load(session) -> Thresholds:
    defaults = _env_defaults()
    rows = {row.name: row.value for row in session.query(AlertSetting).all()}
    if not rows:
        return defaults
    values = {}
    for f in fields(Thresholds):
        raw = rows.get(f.name)
        if raw is None:
            values[f.name] = getattr(defaults, f.name)
        else:
            values[f.name] = int(raw) if f.name in _INT_FIELDS else float(raw)
    return Thresholds(**values)


def get_thresholds(session=None) -> Thresholds:
    """Current effective thresholds. Pass a session when one is in scope
    (handlers); without one a short-lived SessionLocal is used (schema-level
    callers). Serves the cached snapshot inside the TTL."""
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
        logger.exception("Could not refresh alert thresholds — serving previous values")
        fresh = _snapshot if _snapshot is not None else _env_defaults()
    _snapshot = fresh
    _snapshot_at = now
    return fresh


def invalidate() -> None:
    """Drop the cache (called by the PUT endpoint and the test fixture)."""
    global _snapshot, _snapshot_at
    _snapshot = None
    _snapshot_at = 0.0
