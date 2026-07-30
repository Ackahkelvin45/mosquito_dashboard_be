"""Tests for app/notification/events.py — the emit() rule layer."""
from datetime import datetime, timedelta

import pytest

import app.notification.events as events
from app.authentication.enums import UserRole
from app.device.models import MosquitoEvent
from app.notification.enums import NotificationSeverity, NotificationType
from app.notification.events import NotificationEvent, emit
from app.notification.models import Notification


@pytest.fixture
def super_admin(make_user):
    return make_user(role=UserRole.SUPER_ADMIN)


def _rows(db_session, type_=None):
    query = db_session.query(Notification)
    if type_ is not None:
        query = query.filter(Notification.notification_type == type_)
    return query.all()


# ── emit() robustness ────────────────────────────────────────────────────────

class TestEmitNeverRaises:
    def test_unknown_event_string_ignored(self, db_session):
        emit(db_session, "NOT_A_REAL_EVENT")
        assert _rows(db_session) == []

    def test_missing_context_swallowed(self, db_session, super_admin):
        # LOW_BATTERY without device/voltage -> handler TypeError -> swallowed.
        emit(db_session, NotificationEvent.LOW_BATTERY)
        assert _rows(db_session) == []

    def test_bad_context_object_swallowed(self, db_session, super_admin):
        emit(db_session, NotificationEvent.SPECIES_DETECTED, device=object())
        assert _rows(db_session) == []

    def test_no_session_data_swallowed(self, db_session):
        # A session with no users at all — fan-out resolves zero recipients.
        emit(db_session, NotificationEvent.UNKNOWN_DEVICE, device_uuid="x")
        assert _rows(db_session) == []

    def test_master_switch_disables_all(self, db_session, super_admin, monkeypatch):
        # NOTIFY_ENABLED is read from the environment at import time, so the
        # runtime switch is the module constant.
        monkeypatch.setattr(events, "NOTIFY_ENABLED", False)
        emit(db_session, NotificationEvent.UNKNOWN_DEVICE, device_uuid="x")
        assert _rows(db_session) == []

    @pytest.mark.parametrize("raw,expected", [
        ("0", False), ("false", False), ("no", False), ("", False), ("  NO ", False),
        ("1", True), ("true", True), ("yes", True), ("anything", True),
    ])
    def test_env_bool_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("SOME_FLAG", raw)
        assert events._env_bool("SOME_FLAG") is expected

    def test_env_bool_default(self, monkeypatch):
        monkeypatch.delenv("SOME_FLAG", raising=False)
        assert events._env_bool("SOME_FLAG", "1") is True
        assert events._env_bool("SOME_FLAG", "0") is False


# ── SPECIES_DETECTED ─────────────────────────────────────────────────────────

class TestSpeciesDetected:
    def test_non_vector_genus_not_alerted(self, db_session, super_admin, make_device):
        device = make_device()
        emit(db_session, NotificationEvent.SPECIES_DETECTED, device=device,
             species="Toxorhynchites splendens", genus="Toxorhynchites")
        assert _rows(db_session) == []

    @pytest.mark.parametrize("genus", ["Anopheles", "Aedes", "Culex", "aedes", "CULEX"])
    def test_vector_genus_alerts(self, db_session, super_admin, make_device, genus):
        device = make_device()
        emit(db_session, NotificationEvent.SPECIES_DETECTED, device=device, genus=genus)
        rows = _rows(db_session, NotificationType.SPECIES_DETECTED)
        assert len(rows) == 1
        assert rows[0].severity == NotificationSeverity.WARNING

    def test_genus_derived_from_species_when_missing(self, db_session, super_admin,
                                                     make_device):
        device = make_device()
        emit(db_session, NotificationEvent.SPECIES_DETECTED, device=device,
             species="Anopheles gambiae")
        assert len(_rows(db_session, NotificationType.SPECIES_DETECTED)) == 1

    def test_adult_female_escalates_to_critical(self, db_session, super_admin,
                                                make_device):
        device = make_device()
        emit(db_session, NotificationEvent.SPECIES_DETECTED, device=device,
             species="Aedes aegypti", genus="Aedes", sex="female", age_group="adult")
        row = _rows(db_session, NotificationType.SPECIES_DETECTED)[0]
        assert row.severity == NotificationSeverity.CRITICAL
        assert "Adult female" in row.title

    @pytest.mark.parametrize("sex,age_group", [
        ("male", "adult"), ("female", "larva"), (None, None), ("female", None),
    ])
    def test_non_adult_female_stays_warning(self, db_session, super_admin,
                                            make_device, sex, age_group):
        device = make_device()
        emit(db_session, NotificationEvent.SPECIES_DETECTED, device=device,
             species="Aedes aegypti", genus="Aedes", sex=sex, age_group=age_group)
        row = _rows(db_session, NotificationType.SPECIES_DETECTED)[0]
        assert row.severity == NotificationSeverity.WARNING

    def test_payload_and_routing_fields(self, db_session, super_admin, make_device):
        device = make_device()
        emit(db_session, NotificationEvent.SPECIES_DETECTED, device=device,
             species="Aedes aegypti", genus="Aedes", sex="female", age_group="adult")
        row = _rows(db_session, NotificationType.SPECIES_DETECTED)[0]
        assert row.payload["species"] == "Aedes aegypti"
        assert row.payload["device_uuid"] == device.device_uuid
        assert row.device_id == device.id
        assert row.action_url == f"/devices/{device.id}"

    def test_dedupe_window(self, db_session, super_admin, make_device,
                           backdate_dedupe_key):
        device = make_device()
        ctx = dict(device=device, species="Aedes aegypti", genus="Aedes")
        emit(db_session, NotificationEvent.SPECIES_DETECTED, **ctx)
        emit(db_session, NotificationEvent.SPECIES_DETECTED, **ctx)
        assert len(_rows(db_session, NotificationType.SPECIES_DETECTED)) == 1
        backdate_dedupe_key(f"species:{device.id}:aedes aegypti",
                            events.DEDUPE_SPECIES_MIN + 1)
        emit(db_session, NotificationEvent.SPECIES_DETECTED, **ctx)
        assert len(_rows(db_session, NotificationType.SPECIES_DETECTED)) == 2


# ── ACTIVITY_SURGE ───────────────────────────────────────────────────────────

class TestActivitySurge:
    def test_below_or_at_threshold_no_alert(self, db_session, super_admin, make_device):
        device = make_device()
        emit(db_session, NotificationEvent.ACTIVITY_SURGE, device=device,
             count=events.NOTIFY_SURGE_THRESHOLD)  # == threshold: not a surge
        assert _rows(db_session, NotificationType.ACTIVITY_SURGE) == []

    def test_above_threshold_alerts(self, db_session, super_admin, make_device):
        device = make_device()
        emit(db_session, NotificationEvent.ACTIVITY_SURGE, device=device,
             count=events.NOTIFY_SURGE_THRESHOLD + 1)
        rows = _rows(db_session, NotificationType.ACTIVITY_SURGE)
        assert len(rows) == 1
        assert rows[0].payload["count"] == events.NOTIFY_SURGE_THRESHOLD + 1

    def test_count_computed_from_seeded_events(self, db_session, super_admin,
                                               make_device):
        device = make_device()
        now = datetime.utcnow()
        for _ in range(events.NOTIFY_SURGE_THRESHOLD + 1):
            db_session.add(MosquitoEvent(device_id=device.id, timestamp=now, count=1))
        # One stale event outside the window must not count.
        db_session.add(MosquitoEvent(
            device_id=device.id,
            timestamp=now - timedelta(minutes=events.NOTIFY_SURGE_WINDOW_MIN + 5),
            count=1,
        ))
        db_session.commit()
        emit(db_session, NotificationEvent.ACTIVITY_SURGE, device=device)
        rows = _rows(db_session, NotificationType.ACTIVITY_SURGE)
        assert len(rows) == 1
        assert rows[0].payload["count"] == events.NOTIFY_SURGE_THRESHOLD + 1

    def test_computed_count_below_threshold_no_alert(self, db_session, super_admin,
                                                     make_device):
        device = make_device()
        db_session.add(MosquitoEvent(device_id=device.id,
                                     timestamp=datetime.utcnow(), count=1))
        db_session.commit()
        emit(db_session, NotificationEvent.ACTIVITY_SURGE, device=device)
        assert _rows(db_session, NotificationType.ACTIVITY_SURGE) == []

    def test_dedupe_window(self, db_session, super_admin, make_device):
        device = make_device()
        emit(db_session, NotificationEvent.ACTIVITY_SURGE, device=device, count=100)
        emit(db_session, NotificationEvent.ACTIVITY_SURGE, device=device, count=100)
        assert len(_rows(db_session, NotificationType.ACTIVITY_SURGE)) == 1


# ── LOW_BATTERY ──────────────────────────────────────────────────────────────

class TestLowBattery:
    def test_at_or_above_threshold_no_alert(self, db_session, super_admin, make_device):
        device = make_device()
        emit(db_session, NotificationEvent.LOW_BATTERY, device=device,
             voltage=events.NOTIFY_BATTERY_CRITICAL_V)
        emit(db_session, NotificationEvent.LOW_BATTERY, device=device, voltage=4.2)
        emit(db_session, NotificationEvent.LOW_BATTERY, device=device, voltage=None)
        assert _rows(db_session, NotificationType.LOW_BATTERY) == []

    def test_below_threshold_warning(self, db_session, super_admin, make_device):
        device = make_device()
        emit(db_session, NotificationEvent.LOW_BATTERY, device=device, voltage=3.2)
        row = _rows(db_session, NotificationType.LOW_BATTERY)[0]
        assert row.severity == NotificationSeverity.WARNING
        assert row.payload["voltage"] == 3.2

    def test_below_urgent_threshold_critical(self, db_session, super_admin,
                                             make_device):
        device = make_device()
        emit(db_session, NotificationEvent.LOW_BATTERY, device=device, voltage=2.9)
        row = _rows(db_session, NotificationType.LOW_BATTERY)[0]
        assert row.severity == NotificationSeverity.CRITICAL

    def test_exactly_urgent_threshold_is_warning(self, db_session, super_admin,
                                                 make_device):
        device = make_device()
        emit(db_session, NotificationEvent.LOW_BATTERY, device=device,
             voltage=events.BATTERY_URGENT_V)  # 3.0 is not < 3.0
        row = _rows(db_session, NotificationType.LOW_BATTERY)[0]
        assert row.severity == NotificationSeverity.WARNING

    def test_dedupe_window(self, db_session, super_admin, make_device,
                           backdate_dedupe_key):
        device = make_device()
        emit(db_session, NotificationEvent.LOW_BATTERY, device=device, voltage=3.1)
        emit(db_session, NotificationEvent.LOW_BATTERY, device=device, voltage=3.0)
        assert len(_rows(db_session, NotificationType.LOW_BATTERY)) == 1
        backdate_dedupe_key(f"low_battery:{device.id}", events.DEDUPE_BATTERY_MIN + 1)
        emit(db_session, NotificationEvent.LOW_BATTERY, device=device, voltage=3.1)
        assert len(_rows(db_session, NotificationType.LOW_BATTERY)) == 2


# ── Environment bounds ───────────────────────────────────────────────────────

class TestEnvironmentBounds:
    @pytest.mark.parametrize("temperature", [5, 45, 20, None])
    def test_temperature_in_bounds_no_alert(self, db_session, super_admin,
                                            make_device, temperature):
        device = make_device()
        emit(db_session, NotificationEvent.EXTREME_TEMPERATURE, device=device,
             temperature=temperature)
        assert _rows(db_session, NotificationType.EXTREME_TEMPERATURE) == []

    @pytest.mark.parametrize("temperature", [4.9, 45.1, -10, 60])
    def test_temperature_out_of_bounds_alerts(self, db_session, super_admin,
                                              make_device, temperature):
        device = make_device()
        emit(db_session, NotificationEvent.EXTREME_TEMPERATURE, device=device,
             temperature=temperature)
        rows = _rows(db_session, NotificationType.EXTREME_TEMPERATURE)
        assert len(rows) == 1
        assert rows[0].severity == NotificationSeverity.WARNING

    @pytest.mark.parametrize("humidity", [10, 98, 50, None])
    def test_humidity_in_bounds_no_alert(self, db_session, super_admin,
                                         make_device, humidity):
        device = make_device()
        emit(db_session, NotificationEvent.EXTREME_HUMIDITY, device=device,
             humidity=humidity)
        assert _rows(db_session, NotificationType.EXTREME_HUMIDITY) == []

    @pytest.mark.parametrize("humidity", [9.9, 98.1, 0, 100])
    def test_humidity_out_of_bounds_alerts(self, db_session, super_admin,
                                           make_device, humidity):
        device = make_device()
        emit(db_session, NotificationEvent.EXTREME_HUMIDITY, device=device,
             humidity=humidity)
        assert len(_rows(db_session, NotificationType.EXTREME_HUMIDITY)) == 1

    def test_environment_dedupe_windows(self, db_session, super_admin, make_device):
        device = make_device()
        emit(db_session, NotificationEvent.EXTREME_TEMPERATURE, device=device,
             temperature=50)
        emit(db_session, NotificationEvent.EXTREME_TEMPERATURE, device=device,
             temperature=55)
        assert len(_rows(db_session, NotificationType.EXTREME_TEMPERATURE)) == 1
        emit(db_session, NotificationEvent.EXTREME_HUMIDITY, device=device, humidity=99)
        emit(db_session, NotificationEvent.EXTREME_HUMIDITY, device=device, humidity=100)
        assert len(_rows(db_session, NotificationType.EXTREME_HUMIDITY)) == 1


# ── UNKNOWN_DEVICE / INVALID_PAYLOAD / misc events ───────────────────────────

class TestAdminEvents:
    def test_unknown_device_targets_super_admins_only(self, db_session, make_user,
                                                      make_cluster):
        cluster = make_cluster()
        member = make_user(cluster_id=cluster.id)
        admin = make_user(role=UserRole.ADMIN, cluster_id=cluster.id)
        super_admin = make_user(role=UserRole.SUPER_ADMIN)
        emit(db_session, NotificationEvent.UNKNOWN_DEVICE,
             device_uuid="ghost-1", topic="mosquito_dashboard/ghost-1/sensor_data")
        rows = _rows(db_session, NotificationType.UNKNOWN_DEVICE)
        assert [r.user_id for r in rows] == [super_admin.id]
        assert member.id not in {r.user_id for r in rows}
        assert admin.id not in {r.user_id for r in rows}
        assert rows[0].payload["device_uuid"] == "ghost-1"

    def test_unknown_device_dedupe_per_uuid(self, db_session, super_admin):
        emit(db_session, NotificationEvent.UNKNOWN_DEVICE, device_uuid="ghost-1")
        emit(db_session, NotificationEvent.UNKNOWN_DEVICE, device_uuid="ghost-1")
        emit(db_session, NotificationEvent.UNKNOWN_DEVICE, device_uuid="ghost-2")
        assert len(_rows(db_session, NotificationType.UNKNOWN_DEVICE)) == 2

    def test_invalid_payload_targets_super_admins(self, db_session, super_admin,
                                                  make_user):
        make_user()  # regular user must not receive it
        emit(db_session, NotificationEvent.INVALID_PAYLOAD, topic="t", error="boom")
        rows = _rows(db_session, NotificationType.INVALID_PAYLOAD)
        assert [r.user_id for r in rows] == [super_admin.id]
        assert rows[0].payload["error"] == "boom"


class TestDeviceAndAccountEvents:
    def test_device_offline_critical_no_dedupe(self, db_session, super_admin,
                                               make_device):
        device = make_device()
        emit(db_session, NotificationEvent.DEVICE_OFFLINE, device=device)
        emit(db_session, NotificationEvent.DEVICE_OFFLINE, device=device)
        rows = _rows(db_session, NotificationType.DEVICE_OFFLINE)
        # No dedupe window on OFFLINE — the job's state machine is the guard.
        assert len(rows) == 2
        assert all(r.severity == NotificationSeverity.CRITICAL for r in rows)

    def test_device_online_success(self, db_session, super_admin, make_device):
        device = make_device()
        emit(db_session, NotificationEvent.DEVICE_ONLINE, device=device,
             offline_duration_min=12)  # extra ctx tolerated
        rows = _rows(db_session, NotificationType.DEVICE_ONLINE)
        assert len(rows) == 1
        assert rows[0].severity == NotificationSeverity.SUCCESS

    def test_user_registered_goes_to_admins(self, db_session, super_admin, make_user):
        newcomer = make_user()
        emit(db_session, NotificationEvent.USER_REGISTERED, user=newcomer)
        rows = _rows(db_session, NotificationType.USER_REGISTERED)
        assert [r.user_id for r in rows] == [super_admin.id]
        assert rows[0].payload["registered_user_id"] == newcomer.id

    def test_user_approved_goes_to_the_user(self, db_session, make_user):
        user = make_user()
        emit(db_session, NotificationEvent.USER_APPROVED, user=user)
        rows = _rows(db_session, NotificationType.USER_APPROVED)
        assert [r.user_id for r in rows] == [user.id]

    def test_researcher_flow_events(self, db_session, super_admin, make_user,
                                    make_cluster):
        user = make_user()
        cluster = make_cluster()
        emit(db_session, NotificationEvent.RESEARCHER_REQUEST_SUBMITTED,
             user=user, cluster=cluster)
        submitted = _rows(db_session, NotificationType.RESEARCHER_REQUEST_SUBMITTED)
        assert [r.user_id for r in submitted] == [super_admin.id]
        emit(db_session, NotificationEvent.RESEARCHER_REQUEST_APPROVED,
             user=user, cluster=cluster)
        approved = _rows(db_session, NotificationType.RESEARCHER_REQUEST_APPROVED)
        assert [r.user_id for r in approved] == [user.id]
        assert approved[0].payload["cluster_id"] == cluster.id
        emit(db_session, NotificationEvent.RESEARCHER_REQUEST_REJECTED, user=user)
        rejected = _rows(db_session, NotificationType.RESEARCHER_REQUEST_REJECTED)
        assert [r.user_id for r in rejected] == [user.id]

    def test_maintenance_due_dedupe(self, db_session, super_admin, make_device):
        device = make_device()
        emit(db_session, NotificationEvent.MAINTENANCE_DUE, device=device,
             days_inactive=31)
        emit(db_session, NotificationEvent.MAINTENANCE_DUE, device=device,
             days_inactive=31)
        rows = _rows(db_session, NotificationType.MAINTENANCE_DUE)
        assert len(rows) == 1
        assert rows[0].payload["days_inactive"] == 31

    def test_summaries_target_given_user(self, db_session, make_user):
        user = make_user()
        emit(db_session, NotificationEvent.DAILY_SUMMARY, user=user,
             stats={"mosquito_events": 3}, body="custom body")
        row = _rows(db_session, NotificationType.DAILY_SUMMARY)[0]
        assert row.user_id == user.id
        assert row.body == "custom body"
        assert row.payload == {"mosquito_events": 3}
        emit(db_session, NotificationEvent.WEEKLY_SUMMARY, user=user)
        assert len(_rows(db_session, NotificationType.WEEKLY_SUMMARY)) == 1

    def test_cluster_events(self, db_session, super_admin, make_cluster, make_device,
                            make_user):
        cluster = make_cluster()
        member = make_user(cluster_id=cluster.id)
        device = make_device(cluster)
        emit(db_session, NotificationEvent.CLUSTER_CREATED, cluster=cluster)
        emit(db_session, NotificationEvent.CLUSTER_DEVICE_ADDED,
             cluster=cluster, device=device)
        emit(db_session, NotificationEvent.CLUSTER_DEVICE_REMOVED,
             cluster=cluster, device=device)
        emit(db_session, NotificationEvent.CLUSTER_UPDATED, cluster=cluster)
        for type_ in (NotificationType.CLUSTER_CREATED,
                      NotificationType.CLUSTER_DEVICE_ADDED,
                      NotificationType.CLUSTER_DEVICE_REMOVED,
                      NotificationType.CLUSTER_UPDATED):
            recipients = {r.user_id for r in _rows(db_session, type_)}
            assert recipients == {member.id, super_admin.id}, type_

    def test_test_event(self, db_session, make_user):
        user = make_user(role=UserRole.SUPER_ADMIN)
        emit(db_session, NotificationEvent.TEST, user=user)
        assert len(_rows(db_session, NotificationType.TEST)) == 1
