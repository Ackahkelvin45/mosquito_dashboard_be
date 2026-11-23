"""Daily/weekly surveillance summaries.

Daily at NOTIFY_DAILY_SUMMARY_UTC (default 07:00 UTC) and weekly on Monday at
the same time. Per cluster, the last 24h/7d are aggregated (detection events,
mosquito total, top species, device reporting/silent/offline/low-battery
counts, alerts opened/acknowledged/closed in the period + still open) and a
DAILY_SUMMARY / WEEKLY_SUMMARY is emitted per recipient — events.py expects
`user` (+ stats/body) per its ctx docs, so this job resolves the recipients
itself: active cluster members + active cluster admins. Super admins get ONE
global summary across all devices instead of one per cluster (spam control),
so they are excluded from the per-cluster fan-out. Clusters with zero devices
AND zero activity are skipped. Per-user preferences are honoured downstream
(service gates in_app/email; summaries are in EMAIL_AUTO_TYPES).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import String, cast, distinct, func, or_

from app.authentication.enums import UserRole
from app.authentication.models import User
from app.core.database import SessionLocal
from app.device.schema import OFFLINE_AFTER_MIN
from app.device.models import (
    Device,
    DeviceCluster,
    MosquitoEvent,
    MosquitoIndividualReading,
    ANALYSIS_MOSQUITO_EVENT,
)
from app.notification.alert_settings import get_thresholds
from app.notification.enums import HANDLEABLE_TYPES
from app.notification.events import NotificationEvent, emit
from app.notification.models import Notification

logger = logging.getLogger(__name__)


def run_daily_summary() -> None:
    _run_summary(NotificationEvent.DAILY_SUMMARY, hours=24, period_label="24 hours")


def run_weekly_summary() -> None:
    _run_summary(NotificationEvent.WEEKLY_SUMMARY, hours=24 * 7, period_label="7 days")


def _run_summary(event: NotificationEvent, *, hours: int, period_label: str) -> None:
    with SessionLocal() as session:
        since = datetime.utcnow() - timedelta(hours=hours)
        emitted = 0

        for cluster in session.query(DeviceCluster).all():
            stats = {**_device_group_stats(session, cluster.devices, since),
                     **alert_stats(session, since, cluster_id=cluster.id)}
            if not _has_news(stats):
                continue  # nothing to report
            body = _summary_body(f"Cluster '{cluster.name}'", stats, period_label)
            payload = {"cluster_id": cluster.id, "cluster": cluster.name,
                       "period": period_label, **stats}
            for user in _cluster_recipients(session, cluster):
                emit(session, event, user=user, stats=payload, body=body)
                emitted += 1

        # Super admins get one global summary instead of one per cluster.
        all_devices = session.query(Device).all()
        stats = {**_device_group_stats(session, all_devices, since),
                 **alert_stats(session, since)}
        if _has_news(stats):
            body = _summary_body("All clusters", stats, period_label)
            payload = {"cluster_id": None, "scope": "global",
                       "period": period_label, **stats}
            for user in _super_admins(session):
                emit(session, event, user=user, stats=payload, body=body)
                emitted += 1

        logger.info("%s: emitted %d summary notification(s)", event, emitted)


def _has_news(stats: dict) -> bool:
    return bool(stats["device_count"] or stats["mosquito_events"]
                or stats["alerts_opened"] or stats["alerts_acknowledged"]
                or stats["alerts_closed"] or stats["alerts_still_open"])


def detection_stats(session, device_ids: list[int], since: datetime,
                    until: Optional[datetime] = None) -> dict:
    """Live detection events, mosquito total and top species for these devices
    over [since, until) (until=None: up to now). Shared with /reports."""
    if not device_ids:
        return {"mosquito_events": 0, "mosquito_total": 0, "top_species": None}
    window = [
        MosquitoEvent.device_id.in_(device_ids),
        MosquitoEvent.timestamp >= since,
        ANALYSIS_MOSQUITO_EVENT,
    ]
    if until is not None:
        window.append(MosquitoEvent.timestamp < until)
    mosquito_events = (
        session.query(func.count(MosquitoEvent.id)).filter(*window).scalar() or 0
    )
    mosquito_total = (
        session.query(func.coalesce(func.sum(MosquitoEvent.count), 0))
        .filter(*window)
        .scalar()
        or 0
    )
    top = (
        session.query(
            MosquitoIndividualReading.species,
            func.count(MosquitoIndividualReading.id),
        )
        .join(MosquitoEvent, MosquitoIndividualReading.batch_id == MosquitoEvent.id)
        .filter(*window)
        .filter(
            MosquitoIndividualReading.species.isnot(None),
            MosquitoIndividualReading.species != "",
        )
        .group_by(MosquitoIndividualReading.species)
        .order_by(func.count(MosquitoIndividualReading.id).desc())
        .first()
    )
    return {
        "mosquito_events": int(mosquito_events),
        "mosquito_total": int(mosquito_total),
        "top_species": top[0] if top else None,
    }


def alert_stats(session, since: datetime, cluster_id: Optional[int] = None) -> dict:
    """Alerts (HANDLEABLE_TYPES) opened / acknowledged / closed since `since`,
    plus how many are not closed right now. One alert = one emission
    (alert_group), not one per recipient; legacy rows without a group count
    once each. Only alerts with at least one live row count (not deleted, not
    expired — the read-path rule); stamps are shared per group, so filtering
    rows is the same as filtering groups. cluster_id=None counts every alert
    (global summary)."""
    alert_key = distinct(func.coalesce(Notification.alert_group,
                                       cast(Notification.id, String)))
    now = datetime.utcnow()
    base = [
        Notification.notification_type.in_(HANDLEABLE_TYPES),
        Notification.deleted_at.is_(None),
        or_(Notification.expires_at.is_(None), Notification.expires_at > now),
    ]
    if cluster_id is not None:
        base.append(Notification.cluster_id == cluster_id)

    def count(*criteria) -> int:
        return session.query(func.count(alert_key)).filter(*base, *criteria).scalar() or 0

    return {
        "alerts_opened": count(Notification.created_at >= since),
        "alerts_acknowledged": count(Notification.acknowledged_at >= since),
        "alerts_closed": count(Notification.handled_at >= since),
        "alerts_still_open": count(Notification.handled_at.is_(None)),
    }


def _device_group_stats(session, devices, since: datetime) -> dict:
    device_ids = [device.id for device in devices]
    detections = detection_stats(session, device_ids, since)

    offline_devices = sum(1 for device in devices if device.offline_since is not None)
    # Same rule as the device is_active badge: sensor_data within
    # OFFLINE_AFTER_MIN. Never-reported devices count as silent.
    reporting_cutoff = datetime.utcnow() - timedelta(minutes=OFFLINE_AFTER_MIN)
    reporting_devices = sum(
        1 for device in devices
        if device.last_sensor_data_at is not None and device.last_sensor_data_at >= reporting_cutoff
    )
    # Resolved live (never from-imported) so the digest count always agrees
    # with the alert threshold an admin may have edited at runtime.
    battery_critical_v = get_thresholds().battery_critical_v
    low_battery_devices = 0
    for device in devices:
        reading = device.latest_reading  # correlated LIMIT 1 — cheap per device
        if (
            reading is not None
            and reading.battery_voltage is not None
            and reading.battery_voltage < battery_critical_v
        ):
            low_battery_devices += 1

    return {
        "device_count": len(device_ids),
        **detections,
        "offline_devices": offline_devices,
        "reporting_devices": reporting_devices,
        "silent_devices": len(device_ids) - reporting_devices,
        "low_battery_devices": low_battery_devices,
    }


def _summary_body(scope_label: str, stats: dict, period_label: str) -> str:
    species_part = (
        f", top species: {stats['top_species']}" if stats["top_species"] else ""
    )
    return (
        f"{scope_label} over the last {period_label}: "
        f"{stats['mosquito_events']} detection event(s), "
        f"{stats['mosquito_total']} mosquito(es){species_part}. "
        f"Devices: {stats['device_count']} total, "
        f"{stats['reporting_devices']} reporting, "
        f"{stats['silent_devices']} silent, "
        f"{stats['offline_devices']} offline, "
        f"{stats['low_battery_devices']} on low battery. "
        f"Alerts: {stats['alerts_opened']} opened, "
        f"{stats['alerts_acknowledged']} acknowledged, "
        f"{stats['alerts_closed']} closed, "
        f"{stats['alerts_still_open']} still open."
    )


def _cluster_recipients(session, cluster: DeviceCluster) -> list[User]:
    """Active members only, minus super admins (they get the global summary).

    The cluster_admins M2M is deliberately NOT consulted: RBAC_PLAN marks it
    deprecated, and stale rows there can point at users whose cluster_id is a
    different cluster — including them would leak this cluster's stats.
    """
    return (
        session.query(User)
        .filter(
            User.cluster_id == cluster.id,
            User.is_active.is_(True),
            User.role != UserRole.SUPER_ADMIN,
        )
        .all()
    )


def _super_admins(session) -> list[User]:
    return (
        session.query(User)
        .filter(User.role == UserRole.SUPER_ADMIN, User.is_active.is_(True))
        .all()
    )
