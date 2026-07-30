"""Daily/weekly surveillance summaries.

Daily at NOTIFY_DAILY_SUMMARY_UTC (default 07:00 UTC) and weekly on Monday at
the same time. Per cluster, the last 24h/7d are aggregated (detection events,
mosquito total, top species, device/offline/low-battery counts) and a
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

from sqlalchemy import func

from app.authentication.enums import UserRole
from app.authentication.models import User
from app.core.database import SessionLocal
from app.device.models import (
    Device,
    DeviceCluster,
    MosquitoEvent,
    MosquitoIndividualReading,
)
from app.notification.events import NOTIFY_BATTERY_CRITICAL_V, NotificationEvent, emit

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
            stats = _device_group_stats(session, cluster.devices, since)
            if stats["device_count"] == 0 and stats["mosquito_events"] == 0:
                continue  # nothing to report
            body = _summary_body(f"Cluster '{cluster.name}'", stats, period_label)
            payload = {"cluster_id": cluster.id, "cluster": cluster.name,
                       "period": period_label, **stats}
            for user in _cluster_recipients(session, cluster):
                emit(session, event, user=user, stats=payload, body=body)
                emitted += 1

        # Super admins get one global summary instead of one per cluster.
        all_devices = session.query(Device).all()
        stats = _device_group_stats(session, all_devices, since)
        if stats["device_count"] or stats["mosquito_events"]:
            body = _summary_body("All clusters", stats, period_label)
            payload = {"cluster_id": None, "scope": "global",
                       "period": period_label, **stats}
            for user in _super_admins(session):
                emit(session, event, user=user, stats=payload, body=body)
                emitted += 1

        logger.info("%s: emitted %d summary notification(s)", event, emitted)


def _device_group_stats(session, devices, since: datetime) -> dict:
    device_ids = [device.id for device in devices]
    mosquito_events = 0
    mosquito_total = 0
    top_species = None
    if device_ids:
        window = (
            MosquitoEvent.device_id.in_(device_ids),
            MosquitoEvent.timestamp >= since,
        )
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
        top_species = top[0] if top else None

    offline_devices = sum(1 for device in devices if device.offline_since is not None)
    low_battery_devices = 0
    for device in devices:
        reading = device.latest_reading  # correlated LIMIT 1 — cheap per device
        if (
            reading is not None
            and reading.battery_voltage is not None
            and reading.battery_voltage < NOTIFY_BATTERY_CRITICAL_V
        ):
            low_battery_devices += 1

    return {
        "device_count": len(device_ids),
        "mosquito_events": int(mosquito_events),
        "mosquito_total": int(mosquito_total),
        "top_species": top_species,
        "offline_devices": offline_devices,
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
        f"{stats['offline_devices']} offline, "
        f"{stats['low_battery_devices']} on low battery."
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
