"""Notification events — the decoupling layer between business logic and the
notification service.

Business logic (MQTT handlers, auth/researcher/cluster flows, background jobs)
NEVER constructs notifications. It calls:

    from app.notification.events import NotificationEvent, emit
    emit(session, NotificationEvent.LOW_BATTERY, device=device, voltage=3.1)

`emit` NEVER raises: any failure is logged and swallowed, so a notification
bug can never break ingestion or a request. Each event maps to a rule here
(recipients, severity, dedupe window, message template, icon, action_url) per
the trigger matrix in NOTIFICATIONS_PLAN.md §4.

Event context is passed as kwargs. Expected context per event (extra kwargs
are tolerated and ignored):

    SPECIES_DETECTED             device, species?, genus?, sex?, age_group?
    ACTIVITY_SURGE               device, count? (computed from MosquitoEvent when omitted)
    LOW_BATTERY                  device, voltage
    TRAP_TRIGGERED               device            (caller detects the false→true flip)
    EXTREME_TEMPERATURE          device, temperature
    EXTREME_HUMIDITY             device, humidity
    SENSOR_MALFUNCTION           device, reason?
    UNKNOWN_DEVICE               device_uuid, topic?
    INVALID_PAYLOAD              topic?, error?
    DEVICE_OFFLINE               device             (state machine via devices.offline_since)
    DEVICE_ONLINE                device
    DEVICE_LOCATION_CHANGED      device, distance_m?
    DEVICE_REASSIGNED            device, previous_cluster_id?
    USER_REGISTERED              user
    USER_APPROVED                user
    USER_REJECTED                user
    ROLE_CHANGED                 user, role?
    PASSWORD_RESET               user
    RESEARCHER_REQUEST_SUBMITTED user, cluster?
    RESEARCHER_REQUEST_APPROVED  user, cluster?
    RESEARCHER_REQUEST_REJECTED  user
    CLUSTER_CREATED              cluster
    CLUSTER_UPDATED              cluster
    CLUSTER_DEVICE_ADDED         cluster, device
    CLUSTER_DEVICE_REMOVED       cluster, device
    MAINTENANCE_DUE              device, days_inactive?
    DAILY_SUMMARY                user, stats?, body?
    WEEKLY_SUMMARY               user, stats?, body?
    TEST                         user

`device`/`user`/`cluster` are ORM objects (Device / User / DeviceCluster).
"""
import logging
import os
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.device.models import MosquitoEvent
from app.notification.enums import (
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
)
from app.notification.service import NotificationService

logger = logging.getLogger(__name__)

# ── Rule constants (env-tunable, startup-safe defaults per plan §9) ──────────

def _env_bool(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", ""}

# Master switch — flip NOTIFY_ENABLED=0 to silence the whole system.
NOTIFY_ENABLED = _env_bool("NOTIFY_ENABLED", "1")

NOTIFY_BATTERY_CRITICAL_V = float(os.getenv("NOTIFY_BATTERY_CRITICAL_V", "3.3"))
# Below this the LOW_BATTERY escalates from WARNING to CRITICAL.
BATTERY_URGENT_V = 3.0
NOTIFY_SURGE_THRESHOLD = int(os.getenv("NOTIFY_SURGE_THRESHOLD", "20"))
NOTIFY_SURGE_WINDOW_MIN = int(os.getenv("NOTIFY_SURGE_WINDOW_MIN", "60"))
NOTIFY_TEMP_MIN = float(os.getenv("NOTIFY_TEMP_MIN", "5"))
NOTIFY_TEMP_MAX = float(os.getenv("NOTIFY_TEMP_MAX", "45"))
NOTIFY_HUMIDITY_MIN = float(os.getenv("NOTIFY_HUMIDITY_MIN", "10"))
NOTIFY_HUMIDITY_MAX = float(os.getenv("NOTIFY_HUMIDITY_MAX", "98"))

# Disease-vector genera: a detection in one of these genera notifies; an adult
# female escalates to CRITICAL (the ones that bite and transmit).
VECTOR_GENERA = {"anopheles", "aedes", "culex"}

# Dedupe windows (minutes) per plan §4.
DEDUPE_SPECIES_MIN = 30
DEDUPE_SURGE_MIN = 120
DEDUPE_BATTERY_MIN = 360
DEDUPE_TRAP_MIN = 60
DEDUPE_ENVIRONMENT_MIN = 180
DEDUPE_UNKNOWN_DEVICE_MIN = 60
DEDUPE_LOCATION_MIN = 720
DEDUPE_MAINTENANCE_MIN = 1440


class NotificationEvent(StrEnum):
    SPECIES_DETECTED = "SPECIES_DETECTED"
    ACTIVITY_SURGE = "ACTIVITY_SURGE"
    LOW_BATTERY = "LOW_BATTERY"
    TRAP_TRIGGERED = "TRAP_TRIGGERED"
    EXTREME_TEMPERATURE = "EXTREME_TEMPERATURE"
    EXTREME_HUMIDITY = "EXTREME_HUMIDITY"
    SENSOR_MALFUNCTION = "SENSOR_MALFUNCTION"
    UNKNOWN_DEVICE = "UNKNOWN_DEVICE"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    DEVICE_ONLINE = "DEVICE_ONLINE"
    DEVICE_LOCATION_CHANGED = "DEVICE_LOCATION_CHANGED"
    DEVICE_REASSIGNED = "DEVICE_REASSIGNED"
    USER_REGISTERED = "USER_REGISTERED"
    USER_APPROVED = "USER_APPROVED"
    USER_REJECTED = "USER_REJECTED"
    ROLE_CHANGED = "ROLE_CHANGED"
    PASSWORD_RESET = "PASSWORD_RESET"
    RESEARCHER_REQUEST_SUBMITTED = "RESEARCHER_REQUEST_SUBMITTED"
    RESEARCHER_REQUEST_APPROVED = "RESEARCHER_REQUEST_APPROVED"
    RESEARCHER_REQUEST_REJECTED = "RESEARCHER_REQUEST_REJECTED"
    CLUSTER_CREATED = "CLUSTER_CREATED"
    CLUSTER_UPDATED = "CLUSTER_UPDATED"
    CLUSTER_DEVICE_ADDED = "CLUSTER_DEVICE_ADDED"
    CLUSTER_DEVICE_REMOVED = "CLUSTER_DEVICE_REMOVED"
    MAINTENANCE_DUE = "MAINTENANCE_DUE"
    DAILY_SUMMARY = "DAILY_SUMMARY"
    WEEKLY_SUMMARY = "WEEKLY_SUMMARY"
    TEST = "TEST"


def emit(session: Session, event: "NotificationEvent | str", **ctx) -> None:
    """Fire a notification event. NEVER raises.

    One import, one line at the call site:

        emit(session, NotificationEvent.DEVICE_OFFLINE, device=device)
    """
    if not NOTIFY_ENABLED:
        return
    try:
        try:
            event_key = NotificationEvent(event)
        except ValueError:
            logger.warning("Unknown notification event %r — ignored", event)
            return
        handler = _HANDLERS.get(event_key)
        if handler is None:
            logger.warning("No notification handler registered for event %s", event)
            return
        handler(NotificationService(session), **ctx)
    except Exception:
        logger.exception("Notification emit failed for event %s", event)
        # A failed flush/commit inside the handler leaves the CALLER's session
        # in "pending rollback" state — every later use of it would raise
        # PendingRollbackError and 500 the route / kill the MQTT handler.
        # Only roll back when the session is actually poisoned (is_active is
        # False) so a benign handler bug never discards the caller's own
        # uncommitted work.
        try:
            if not session.is_active:
                session.rollback()
        except Exception:
            logger.exception("Session rollback after failed emit also failed")


# ── Handlers (one per event; extra ctx kwargs are tolerated via **_) ─────────

def _species_detected(service, *, device, species=None, genus=None, sex=None,
                      age_group=None, **_):
    genus_value = (genus or "").strip()
    if not genus_value and species:
        genus_value = species.strip().split()[0]
    if genus_value.lower() not in VECTOR_GENERA:
        return  # non-vector species are recorded, not alerted

    sex_value = (sex or "").strip().lower()
    age_value = (age_group or "").strip().lower()
    is_adult_female = sex_value.startswith("f") and "adult" in age_value
    label = (species or genus_value or "mosquito").strip()

    if is_adult_female:
        severity = NotificationSeverity.CRITICAL
        title = f"⚠️ Adult female {label} detected at {device.name}"
    else:
        severity = NotificationSeverity.WARNING
        title = f"Vector species {label} detected at {device.name}"
    body = (
        f"A {label}"
        + (f" ({sex}, {age_group})" if sex or age_group else "")
        + f" was detected by {device.name}"
        + (f" in {device.region}" if device.region else "")
        + ". Vector species can transmit disease — review the detection."
    )

    service.notify_cluster(
        device.cluster_id,
        title=title,
        body=body,
        notification_type=NotificationType.SPECIES_DETECTED,
        severity=severity,
        category=NotificationCategory.MOSQUITO,
        payload={"species": species, "genus": genus, "sex": sex,
                 "age_group": age_group, "device_uuid": device.device_uuid},
        icon="bug",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
        dedupe_key=f"species:{device.id}:{label.lower()}",
        dedupe_window_minutes=DEDUPE_SPECIES_MIN,
    )


def _activity_surge(service, *, device, count=None, **_):
    if count is None:
        since = datetime.utcnow() - timedelta(minutes=NOTIFY_SURGE_WINDOW_MIN)
        count = (
            service.session.query(func.count(MosquitoEvent.id))
            .filter(MosquitoEvent.device_id == device.id, MosquitoEvent.timestamp >= since)
            .scalar()
            or 0
        )
    if count <= NOTIFY_SURGE_THRESHOLD:
        return

    service.notify_cluster(
        device.cluster_id,
        title=f"Mosquito activity surge at {device.name}",
        body=(
            f"{count} mosquito detections in the last {NOTIFY_SURGE_WINDOW_MIN} minutes "
            f"at {device.name} (threshold: {NOTIFY_SURGE_THRESHOLD}). "
            "Unusually high activity may indicate a breeding site nearby."
        ),
        notification_type=NotificationType.ACTIVITY_SURGE,
        severity=NotificationSeverity.WARNING,
        category=NotificationCategory.MOSQUITO,
        payload={"count": count, "window_minutes": NOTIFY_SURGE_WINDOW_MIN,
                 "threshold": NOTIFY_SURGE_THRESHOLD, "device_uuid": device.device_uuid},
        icon="bug",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
        dedupe_key=f"surge:{device.id}",
        dedupe_window_minutes=DEDUPE_SURGE_MIN,
    )


def _low_battery(service, *, device, voltage, **_):
    if voltage is None or voltage >= NOTIFY_BATTERY_CRITICAL_V:
        return
    critical = voltage < BATTERY_URGENT_V
    service.notify_cluster(
        device.cluster_id,
        title=f"Low battery on {device.name} ({voltage:.2f}V)",
        body=(
            f"Battery voltage on {device.name} dropped to {voltage:.2f}V "
            f"(alert threshold {NOTIFY_BATTERY_CRITICAL_V}V). "
            + ("The device may shut down imminently — replace the battery now."
               if critical else "Plan a battery replacement soon.")
        ),
        notification_type=NotificationType.LOW_BATTERY,
        severity=NotificationSeverity.CRITICAL if critical else NotificationSeverity.WARNING,
        category=NotificationCategory.SENSOR,
        payload={"voltage": voltage, "device_uuid": device.device_uuid},
        icon="battery-low",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
        dedupe_key=f"low_battery:{device.id}",
        dedupe_window_minutes=DEDUPE_BATTERY_MIN,
    )


def _trap_triggered(service, *, device, **_):
    service.notify_cluster(
        device.cluster_id,
        title=f"Trap activated on {device.name}",
        body=f"The trap on {device.name} switched on (trap_status went active).",
        notification_type=NotificationType.TRAP_TRIGGERED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.SENSOR,
        payload={"device_uuid": device.device_uuid},
        icon="zap",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
        dedupe_key=f"trap:{device.id}",
        dedupe_window_minutes=DEDUPE_TRAP_MIN,
    )


def _extreme_temperature(service, *, device, temperature, **_):
    if temperature is None or NOTIFY_TEMP_MIN <= temperature <= NOTIFY_TEMP_MAX:
        return
    service.notify_cluster(
        device.cluster_id,
        title=f"Extreme temperature at {device.name} ({temperature:.1f}°C)",
        body=(
            f"{device.name} reported {temperature:.1f}°C, outside the expected "
            f"range of {NOTIFY_TEMP_MIN:.0f}–{NOTIFY_TEMP_MAX:.0f}°C. "
            "Check the device environment and sensor health."
        ),
        notification_type=NotificationType.EXTREME_TEMPERATURE,
        severity=NotificationSeverity.WARNING,
        category=NotificationCategory.SENSOR,
        payload={"temperature": temperature, "device_uuid": device.device_uuid},
        icon="thermometer",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
        dedupe_key=f"temp:{device.id}",
        dedupe_window_minutes=DEDUPE_ENVIRONMENT_MIN,
    )


def _extreme_humidity(service, *, device, humidity, **_):
    if humidity is None or NOTIFY_HUMIDITY_MIN <= humidity <= NOTIFY_HUMIDITY_MAX:
        return
    service.notify_cluster(
        device.cluster_id,
        title=f"Extreme humidity at {device.name} ({humidity:.0f}%)",
        body=(
            f"{device.name} reported {humidity:.0f}% humidity, outside the expected "
            f"range of {NOTIFY_HUMIDITY_MIN:.0f}–{NOTIFY_HUMIDITY_MAX:.0f}%. "
            "Check the device environment and sensor health."
        ),
        notification_type=NotificationType.EXTREME_HUMIDITY,
        severity=NotificationSeverity.WARNING,
        category=NotificationCategory.SENSOR,
        payload={"humidity": humidity, "device_uuid": device.device_uuid},
        icon="droplets",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
        dedupe_key=f"humidity:{device.id}",
        dedupe_window_minutes=DEDUPE_ENVIRONMENT_MIN,
    )


def _sensor_malfunction(service, *, device, reason=None, **_):
    service.notify_cluster(
        device.cluster_id,
        title=f"Possible sensor malfunction on {device.name}",
        body=(
            f"{device.name} reported implausible or missing sensor values"
            + (f": {reason}" if reason else "")
            + ". The sensor may need inspection."
        ),
        notification_type=NotificationType.SENSOR_MALFUNCTION,
        severity=NotificationSeverity.WARNING,
        category=NotificationCategory.SENSOR,
        payload={"reason": reason, "device_uuid": device.device_uuid},
        icon="alert-triangle",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
        dedupe_key=f"malfunction:{device.id}",
        dedupe_window_minutes=DEDUPE_ENVIRONMENT_MIN,
    )


def _unknown_device(service, *, device_uuid, topic=None, **_):
    service.notify_admins(
        title=f"Unregistered device reporting: {device_uuid}",
        body=(
            f"MQTT data arrived for device UUID {device_uuid}"
            + (f" on topic {topic}" if topic else "")
            + ", but no such device is registered. Its data is being dropped — "
            "register the device to start capturing it."
        ),
        notification_type=NotificationType.UNKNOWN_DEVICE,
        severity=NotificationSeverity.WARNING,
        category=NotificationCategory.SYSTEM,
        payload={"device_uuid": device_uuid, "topic": topic},
        icon="alert-triangle",
        action_url="/devices",
        dedupe_key=f"unknown_device:{device_uuid}",
        dedupe_window_minutes=DEDUPE_UNKNOWN_DEVICE_MIN,
    )


def _invalid_payload(service, *, topic=None, error=None, **_):
    service.notify_admins(
        title="Malformed MQTT payload received",
        body=(
            "A message could not be parsed"
            + (f" on topic {topic}" if topic else "")
            + (f" ({error})" if error else "")
            + ". Check the device firmware or topic configuration."
        ),
        notification_type=NotificationType.INVALID_PAYLOAD,
        severity=NotificationSeverity.WARNING,
        category=NotificationCategory.SYSTEM,
        payload={"topic": topic, "error": str(error)[:500] if error else None},
        icon="alert-triangle",
        action_url="/devices",
        dedupe_key=f"invalid_payload:{topic}",
        dedupe_window_minutes=DEDUPE_UNKNOWN_DEVICE_MIN,
    )


def _device_offline(service, *, device, **_):
    # No dedupe window: the offline job's state machine (devices.offline_since)
    # guarantees exactly one OFFLINE per outage.
    service.notify_cluster(
        device.cluster_id,
        title=f"Device offline: {device.name}",
        body=(
            f"{device.name} has stopped reporting data"
            + (f" (last activity {device.last_activity:%Y-%m-%d %H:%M} UTC)"
               if device.last_activity else "")
            + ". Check power, battery and connectivity."
        ),
        notification_type=NotificationType.DEVICE_OFFLINE,
        severity=NotificationSeverity.CRITICAL,
        category=NotificationCategory.DEVICE,
        payload={"device_uuid": device.device_uuid},
        icon="wifi-off",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
    )


def _device_online(service, *, device, **_):
    service.notify_cluster(
        device.cluster_id,
        title=f"Device back online: {device.name}",
        body=f"{device.name} is reporting data again.",
        notification_type=NotificationType.DEVICE_ONLINE,
        severity=NotificationSeverity.SUCCESS,
        category=NotificationCategory.DEVICE,
        payload={"device_uuid": device.device_uuid},
        icon="wifi",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
    )


def _device_location_changed(service, *, device, distance_m=None, **_):
    service.notify_cluster(
        device.cluster_id,
        title=f"Device moved: {device.name}",
        body=(
            f"{device.name} reported a new position"
            + (f" about {distance_m:.0f}m from its previous one" if distance_m else "")
            + ". If the device was not intentionally relocated, investigate."
        ),
        notification_type=NotificationType.DEVICE_LOCATION_CHANGED,
        severity=NotificationSeverity.WARNING,
        category=NotificationCategory.DEVICE,
        payload={"device_uuid": device.device_uuid, "distance_m": distance_m,
                 "latitude": device.latitude, "longitude": device.longitude},
        icon="map-pin",
        action_url=f"/map/sensor/{device.id}",
        device_id=device.id,
        dedupe_key=f"location:{device.id}",
        dedupe_window_minutes=DEDUPE_LOCATION_MIN,
    )


def _device_reassigned(service, *, device, previous_cluster_id=None, **_):
    service.notify_cluster(
        device.cluster_id,
        title=f"Device reassigned: {device.name}",
        body=f"{device.name} was moved to a different cluster.",
        notification_type=NotificationType.DEVICE_REASSIGNED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.DEVICE,
        payload={"device_uuid": device.device_uuid,
                 "previous_cluster_id": previous_cluster_id},
        icon="boxes",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
    )


def _user_registered(service, *, user, **_):
    service.notify_admins(
        title=f"New user registration: {user.full_name}",
        body=f"{user.full_name} ({user.email}) signed up and is awaiting approval.",
        notification_type=NotificationType.USER_REGISTERED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.ACCOUNT,
        payload={"registered_user_id": user.id, "email": user.email},
        icon="user-plus",
        action_url="/users",
    )


def _user_approved(service, *, user, **_):
    service.notify_user(
        user.id,
        title="Your account has been approved",
        body="An administrator approved your account. You now have full access to the dashboard.",
        notification_type=NotificationType.USER_APPROVED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.ACCOUNT,
        icon="user-check",
    )


def _user_rejected(service, *, user, **_):
    service.notify_user(
        user.id,
        title="Your account was not approved",
        body="An administrator reviewed your registration and did not approve it. "
             "Contact support if you believe this is an error.",
        notification_type=NotificationType.USER_REJECTED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.ACCOUNT,
        icon="user-x",
    )


def _role_changed(service, *, user, role=None, **_):
    role_label = str(role) if role else str(user.role)
    service.notify_user(
        user.id,
        title="Your role has changed",
        body=f"An administrator changed your role to {role_label}.",
        notification_type=NotificationType.ROLE_CHANGED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.ACCOUNT,
        payload={"role": role_label},
        icon="shield",
    )


def _password_reset(service, *, user, **_):
    service.notify_user(
        user.id,
        title="Password reset requested",
        body="A password reset was just requested for your account. If this "
             "wasn't you, you can ignore the email — your password is unchanged.",
        notification_type=NotificationType.PASSWORD_RESET,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.ACCOUNT,
        icon="key-round",
    )


def _researcher_request_submitted(service, *, user, cluster=None, **_):
    service.notify_admins(
        title=f"Researcher request from {user.full_name}",
        body=f"{user.full_name} ({user.email}) submitted a researcher access request"
             + (f" for cluster '{cluster.name}'" if cluster is not None else "")
             + " and is awaiting review.",
        notification_type=NotificationType.RESEARCHER_REQUEST_SUBMITTED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.RESEARCH,
        payload={"requester_user_id": user.id, "email": user.email},
        icon="flask-conical",
        action_url="/approval/research",
    )


def _researcher_request_approved(service, *, user, cluster=None, **_):
    service.notify_user(
        user.id,
        title="Researcher request approved",
        body="Your researcher access request was approved."
             + (f" You now have access to cluster '{cluster.name}'." if cluster is not None else ""),
        notification_type=NotificationType.RESEARCHER_REQUEST_APPROVED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.RESEARCH,
        payload={"cluster_id": cluster.id if cluster is not None else None},
        icon="flask-conical",
    )


def _researcher_request_rejected(service, *, user, **_):
    service.notify_user(
        user.id,
        title="Researcher request declined",
        body="Your researcher access request was declined. "
             "Contact support if you would like more information.",
        notification_type=NotificationType.RESEARCHER_REQUEST_REJECTED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.RESEARCH,
        icon="flask-conical",
    )


def _cluster_created(service, *, cluster, **_):
    service.notify_cluster(
        cluster.id,
        title=f"Cluster created: {cluster.name}",
        body=f"The device cluster '{cluster.name}' was created.",
        notification_type=NotificationType.CLUSTER_CREATED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.CLUSTER,
        icon="boxes",
        action_url="/devices",
    )


def _cluster_updated(service, *, cluster, **_):
    service.notify_cluster(
        cluster.id,
        title=f"Cluster updated: {cluster.name}",
        body=f"The device cluster '{cluster.name}' was updated.",
        notification_type=NotificationType.CLUSTER_UPDATED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.CLUSTER,
        icon="boxes",
        action_url="/devices",
    )


def _cluster_device_added(service, *, cluster, device, **_):
    service.notify_cluster(
        cluster.id,
        title=f"Device added to {cluster.name}",
        body=f"{device.name} was added to the cluster '{cluster.name}'.",
        notification_type=NotificationType.CLUSTER_DEVICE_ADDED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.CLUSTER,
        payload={"device_uuid": device.device_uuid},
        icon="boxes",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
    )


def _cluster_device_removed(service, *, cluster, device, **_):
    # Deletion call sites pass a device snapshot with id=None (the row is
    # gone and notifications.device_id is a hard FK) — never build a dead
    # "/devices/None" link from it.
    device_id = getattr(device, "id", None)
    service.notify_cluster(
        cluster.id,
        title=f"Device removed from {cluster.name}",
        body=f"{device.name} was removed from the cluster '{cluster.name}'.",
        notification_type=NotificationType.CLUSTER_DEVICE_REMOVED,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.CLUSTER,
        payload={"device_uuid": device.device_uuid},
        icon="boxes",
        action_url=f"/devices/{device_id}" if device_id is not None else None,
        device_id=device_id,
    )


def _maintenance_due(service, *, device, days_inactive=None, **_):
    service.notify_cluster(
        device.cluster_id,
        title=f"Maintenance due: {device.name}",
        body=(
            f"{device.name} has had no activity"
            + (f" for {days_inactive} days" if days_inactive else " for an extended period")
            + " and may need maintenance."
        ),
        notification_type=NotificationType.MAINTENANCE_DUE,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.DEVICE,
        payload={"device_uuid": device.device_uuid, "days_inactive": days_inactive},
        icon="wrench",
        action_url=f"/devices/{device.id}",
        device_id=device.id,
        dedupe_key=f"maintenance:{device.id}",
        dedupe_window_minutes=DEDUPE_MAINTENANCE_MIN,
    )


def _daily_summary(service, *, user, stats=None, body=None, **_):
    service.notify_user(
        user.id,
        title="Your daily surveillance summary",
        body=body or "Your daily mosquito surveillance summary is ready.",
        notification_type=NotificationType.DAILY_SUMMARY,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.SYSTEM,
        payload=stats,
        icon="calendar",
        action_url="/notifications",
    )


def _weekly_summary(service, *, user, stats=None, body=None, **_):
    service.notify_user(
        user.id,
        title="Your weekly surveillance summary",
        body=body or "Your weekly mosquito surveillance summary is ready.",
        notification_type=NotificationType.WEEKLY_SUMMARY,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.SYSTEM,
        payload=stats,
        icon="calendar",
        action_url="/notifications",
    )


def _test(service, *, user, **_):
    service.send_test(user.id)


_HANDLERS = {
    NotificationEvent.SPECIES_DETECTED: _species_detected,
    NotificationEvent.ACTIVITY_SURGE: _activity_surge,
    NotificationEvent.LOW_BATTERY: _low_battery,
    NotificationEvent.TRAP_TRIGGERED: _trap_triggered,
    NotificationEvent.EXTREME_TEMPERATURE: _extreme_temperature,
    NotificationEvent.EXTREME_HUMIDITY: _extreme_humidity,
    NotificationEvent.SENSOR_MALFUNCTION: _sensor_malfunction,
    NotificationEvent.UNKNOWN_DEVICE: _unknown_device,
    NotificationEvent.INVALID_PAYLOAD: _invalid_payload,
    NotificationEvent.DEVICE_OFFLINE: _device_offline,
    NotificationEvent.DEVICE_ONLINE: _device_online,
    NotificationEvent.DEVICE_LOCATION_CHANGED: _device_location_changed,
    NotificationEvent.DEVICE_REASSIGNED: _device_reassigned,
    NotificationEvent.USER_REGISTERED: _user_registered,
    NotificationEvent.USER_APPROVED: _user_approved,
    NotificationEvent.USER_REJECTED: _user_rejected,
    NotificationEvent.ROLE_CHANGED: _role_changed,
    NotificationEvent.PASSWORD_RESET: _password_reset,
    NotificationEvent.RESEARCHER_REQUEST_SUBMITTED: _researcher_request_submitted,
    NotificationEvent.RESEARCHER_REQUEST_APPROVED: _researcher_request_approved,
    NotificationEvent.RESEARCHER_REQUEST_REJECTED: _researcher_request_rejected,
    NotificationEvent.CLUSTER_CREATED: _cluster_created,
    NotificationEvent.CLUSTER_UPDATED: _cluster_updated,
    NotificationEvent.CLUSTER_DEVICE_ADDED: _cluster_device_added,
    NotificationEvent.CLUSTER_DEVICE_REMOVED: _cluster_device_removed,
    NotificationEvent.MAINTENANCE_DUE: _maintenance_due,
    NotificationEvent.DAILY_SUMMARY: _daily_summary,
    NotificationEvent.WEEKLY_SUMMARY: _weekly_summary,
    NotificationEvent.TEST: _test,
}
