"""FR-18: DB-backed global alert thresholds (Phase A) and filter-only
personal thresholds (Phase B)."""
import pytest

from app.audit.models import AuditAction, AuditLog
from app.authentication.enums import UserRole
from app.notification import alert_settings, events
from app.notification.alert_settings import AlertSetting, get_thresholds
from app.notification.enums import NotificationType
from app.notification.models import Notification
from app.notification.service import NotificationService


@pytest.fixture
def service(db_session):
    return NotificationService(db_session)


def _rows(db_session, type_):
    return db_session.query(Notification).filter(
        Notification.notification_type == type_).all()


class TestGlobalThresholds:
    def test_defaults_mirror_env_globals(self, db_session):
        limits = get_thresholds(db_session)
        assert limits.temp_max == events.NOTIFY_TEMP_MAX
        assert limits.surge_threshold == events.NOTIFY_SURGE_THRESHOLD
        assert limits.battery_critical_v == events.NOTIFY_BATTERY_CRITICAL_V

    def test_db_row_overrides_and_cache_invalidation(self, db_session):
        db_session.add(AlertSetting(name="temp_max", value=30.0))
        db_session.commit()
        alert_settings.invalidate()
        assert get_thresholds(db_session).temp_max == 30.0
        # Other values still come from the defaults.
        assert get_thresholds(db_session).temp_min == events.NOTIFY_TEMP_MIN

    def test_handler_uses_live_threshold(self, db_session, make_user, make_device,
                                         service):
        make_user(role=UserRole.SUPER_ADMIN)
        device = make_device()
        # 40°C is inside the default range (5–45): no alert.
        events.emit(db_session, events.NotificationEvent.EXTREME_TEMPERATURE,
                    device=device, temperature=40.0)
        assert _rows(db_session, NotificationType.EXTREME_TEMPERATURE) == []
        # Lower the global max to 35 — the same reading now alerts, no restart.
        db_session.add(AlertSetting(name="temp_max", value=35.0))
        db_session.commit()
        alert_settings.invalidate()
        events.emit(db_session, events.NotificationEvent.EXTREME_TEMPERATURE,
                    device=device, temperature=40.0)
        rows = _rows(db_session, NotificationType.EXTREME_TEMPERATURE)
        assert len(rows) == 1
        assert "35" in rows[0].body  # body reflects the live threshold


class TestAlertSettingsApi:
    def test_super_admin_only(self, client, login_as, make_user):
        login_as(make_user(role=UserRole.ADMIN))
        assert client.get("/notifications/alert-settings").status_code == 403
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        assert client.get("/notifications/alert-settings").status_code == 200

    def test_put_validates_and_takes_effect(self, client, login_as, make_user,
                                            db_session):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        assert client.put("/notifications/alert-settings",
                          json={"nonsense": 1}).status_code == 422
        assert client.put("/notifications/alert-settings",
                          json={"temp_max": 999}).status_code == 422
        assert client.put("/notifications/alert-settings",
                          json={"temp_min": 50, "temp_max": 40}).status_code == 422
        res = client.put("/notifications/alert-settings", json={"temp_max": 38})
        assert res.status_code == 200
        assert res.json()["values"]["temp_max"] == 38
        assert client.get("/notifications/alert-settings").json()["values"]["temp_max"] == 38
        assert any(r.action == AuditAction.ALERT_SETTINGS_UPDATED
                   for r in db_session.query(AuditLog).all())


class TestPersonalThresholds:
    def test_filter_only_recipient_drop(self, db_session, make_user, make_device,
                                        service):
        """Global breach at 47°C: the strict user (personal 50) is skipped,
        the default user still gets it."""
        default_user = make_user(role=UserRole.SUPER_ADMIN)
        strict_user = make_user(role=UserRole.SUPER_ADMIN)
        service.update_preferences(
            strict_user.id,
            __import__("app.notification.schema", fromlist=["x"]).NotificationPreferenceUpdate(
                personal_temp_max=50.0),
        )
        device = make_device()
        events.emit(db_session, events.NotificationEvent.EXTREME_TEMPERATURE,
                    device=device, temperature=47.0)
        recipients = {r.user_id for r in
                      _rows(db_session, NotificationType.EXTREME_TEMPERATURE)}
        assert default_user.id in recipients
        assert strict_user.id not in recipients

    def test_personal_bar_crossed_delivers(self, db_session, make_user,
                                           make_device, service):
        from app.notification.schema import NotificationPreferenceUpdate
        user = make_user(role=UserRole.SUPER_ADMIN)
        service.update_preferences(user.id,
                                   NotificationPreferenceUpdate(personal_temp_max=50.0))
        device = make_device()
        events.emit(db_session, events.NotificationEvent.EXTREME_TEMPERATURE,
                    device=device, temperature=55.0)
        assert {r.user_id for r in
                _rows(db_session, NotificationType.EXTREME_TEMPERATURE)} == {user.id}

    def test_low_side_breach_ignores_personal_max(self, db_session, make_user,
                                                  make_device, service):
        from app.notification.schema import NotificationPreferenceUpdate
        user = make_user(role=UserRole.SUPER_ADMIN)
        service.update_preferences(user.id,
                                   NotificationPreferenceUpdate(personal_temp_max=50.0))
        device = make_device()
        # 0°C breaches the LOW bound — personal max must not suppress it.
        events.emit(db_session, events.NotificationEvent.EXTREME_TEMPERATURE,
                    device=device, temperature=0.0)
        assert len(_rows(db_session, NotificationType.EXTREME_TEMPERATURE)) == 1

    def test_tighter_than_global_rejected(self, client, login_as, make_user):
        login_as(make_user())
        # Global temp_max default is 45; 30 would be tighter-than-global.
        res = client.put("/notifications/preferences",
                         json={"personal_temp_max": 30})
        assert res.status_code == 422

    def test_null_clears_back_to_global(self, client, login_as, make_user):
        login_as(make_user())
        assert client.put("/notifications/preferences",
                          json={"personal_temp_max": 50}).status_code == 200
        assert client.get("/notifications/preferences").json()["personal_temp_max"] == 50
        assert client.put("/notifications/preferences",
                          json={"personal_temp_max": None}).status_code == 200
        assert client.get("/notifications/preferences").json()["personal_temp_max"] is None

    def test_battery_personal_voltage(self, db_session, make_user, make_device,
                                      service):
        from app.notification.schema import NotificationPreferenceUpdate
        relaxed = make_user(role=UserRole.SUPER_ADMIN)
        service.update_preferences(relaxed.id,
                                   NotificationPreferenceUpdate(personal_battery_min_v=3.0))
        other = make_user(role=UserRole.SUPER_ADMIN)
        device = make_device()
        # 3.1V breaches global (3.3) but not the relaxed user's 3.0.
        events.emit(db_session, events.NotificationEvent.LOW_BATTERY,
                    device=device, voltage=3.1)
        recipients = {r.user_id for r in _rows(db_session, NotificationType.LOW_BATTERY)}
        assert other.id in recipients
        assert relaxed.id not in recipients
