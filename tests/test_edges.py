"""Edge/branch coverage: model reprs, ABC contracts, repository edge inputs,
emit's poisoned-session recovery, remaining event handlers, dispatch failure
path, and the scheduler's error loop."""
import asyncio
from datetime import datetime

import pytest

import app.notification.events as events
from app.authentication.enums import UserRole
from app.notification.channels.base import NotificationChannel
from app.notification.enums import DeliveryChannel, DeliveryStatus, NotificationType
from app.notification.events import NotificationEvent, emit
from app.notification.models import Notification
from app.notification.providers.base import PushProviderBase
from app.notification.repository.notification_repository import (
    NotificationDeliveryRepository,
    NotificationPreferenceRepository,
    NotificationRepository,
)


class TestModelReprs:
    def test_reprs(self, make_user, make_notification, db_session, service):
        user = make_user()
        n = make_notification(user)
        assert f"id={n.id}" in repr(n)
        preference = service.preference_repository.get_or_create(user.id)
        assert f"user_id={user.id}" in repr(preference)
        from app.notification.models import NotificationDelivery, PushSubscription

        subscription = PushSubscription(user_id=user.id, endpoint="e")
        db_session.add(subscription)
        db_session.commit()
        assert "PushSubscription" in repr(subscription)
        delivery = NotificationDeliveryRepository(db_session).create(
            n.id, DeliveryChannel.PUSH
        )
        assert "NotificationDelivery" in repr(delivery)


class TestAbstractContracts:
    def test_channel_abc_raises(self, db_session):
        class Passthrough(NotificationChannel):
            def send(self, notification, user):
                return super().send(notification, user)

        with pytest.raises(NotImplementedError):
            Passthrough(db_session).send(None, None)

    def test_provider_abc_raises(self):
        class Passthrough(PushProviderBase):
            def send(self, subscription, payload):
                return super().send(subscription, payload)

        with pytest.raises(NotImplementedError):
            Passthrough().send(None, {})


class TestRepositoryEdges:
    def test_create_bulk_empty(self, db_session):
        assert NotificationRepository(db_session).create_bulk([]) == []

    def test_get_by_users_empty(self, db_session):
        assert NotificationPreferenceRepository(db_session).get_by_users([]) == {}

    def test_get_or_create_integrity_race_recovers(self, db_session, monkeypatch,
                                                   make_user):
        """Deterministic replay of the create race: the first existence check
        misses, a competitor inserts the row, our INSERT hits the unique
        constraint -> rollback + adopt the winner's row."""
        from app.notification.models import NotificationPreference

        user = make_user()
        repo = NotificationPreferenceRepository(db_session)
        real_get_by_user = repo.get_by_user
        state = {"calls": 0}

        def racing_get_by_user(user_id):
            state["calls"] += 1
            if state["calls"] == 1:
                # Competitor wins the race while we saw "no row".
                db_session.add(NotificationPreference(user_id=user_id))
                db_session.commit()
                return None
            return real_get_by_user(user_id)

        monkeypatch.setattr(repo, "get_by_user", racing_get_by_user)
        preference = repo.get_or_create(user.id)
        assert preference.user_id == user.id
        assert state["calls"] == 2  # re-read after IntegrityError
        assert db_session.query(NotificationPreference).filter(
            NotificationPreference.user_id == user.id
        ).count() == 1

    def test_list_failed_selects_under_cap(self, db_session, make_user,
                                           make_notification):
        from app.notification.models import NotificationDelivery

        user = make_user()
        n = make_notification(user)
        repo = NotificationDeliveryRepository(db_session)
        under = repo.create(n.id, DeliveryChannel.PUSH)
        repo.mark_attempt(under, DeliveryStatus.FAILED, error="x")
        capped = NotificationDelivery(
            notification_id=n.id, channel=DeliveryChannel.PUSH,
            status=DeliveryStatus.FAILED, attempts=5,
            last_attempt_at=datetime.utcnow(),
        )
        sent = NotificationDelivery(
            notification_id=n.id, channel=DeliveryChannel.EMAIL,
            status=DeliveryStatus.SENT, attempts=1,
        )
        db_session.add_all([capped, sent])
        db_session.commit()
        failed = repo.list_failed(max_attempts=5)
        assert [d.id for d in failed] == [under.id]


class TestEmitRecovery:
    def test_no_handler_registered_warns_and_returns(self, db_session, monkeypatch,
                                                     make_user):
        make_user(role=UserRole.SUPER_ADMIN)
        monkeypatch.delitem(events._HANDLERS, NotificationEvent.TEST)
        emit(db_session, NotificationEvent.TEST, user=None)  # no raise, no row
        assert db_session.query(Notification).count() == 0

    def test_poisoned_session_rolled_back(self, db_session, monkeypatch, make_user):
        user = make_user()

        def poison(service, **_ctx):
            # NOT NULL violation on flush leaves the session pending-rollback.
            service.session.add(Notification(user_id=None, title="x", body="x"))
            service.session.flush()

        monkeypatch.setitem(events._HANDLERS, NotificationEvent.TEST, poison)
        emit(db_session, NotificationEvent.TEST, user=user)
        # The caller's session must be usable again immediately.
        assert db_session.query(Notification).count() == 0

    def test_rollback_failure_also_swallowed(self, db_session, monkeypatch, make_user):
        user = make_user()

        def poison(service, **_ctx):
            service.session.add(Notification(user_id=None, title="x", body="x"))
            service.session.flush()

        monkeypatch.setitem(events._HANDLERS, NotificationEvent.TEST, poison)

        def broken_rollback():
            raise RuntimeError("rollback failed too")

        monkeypatch.setattr(db_session, "rollback", broken_rollback)
        emit(db_session, NotificationEvent.TEST, user=user)  # still no raise


class TestRemainingEventHandlers:
    @pytest.fixture
    def super_admin(self, make_user):
        return make_user(role=UserRole.SUPER_ADMIN)

    def test_device_location_changed(self, db_session, super_admin, make_device):
        device = make_device(latitude=5.6, longitude=-0.1)
        emit(db_session, NotificationEvent.DEVICE_LOCATION_CHANGED,
             device=device, distance_m=120.0)
        rows = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.DEVICE_LOCATION_CHANGED
        ).all()
        assert len(rows) == 1
        assert rows[0].payload["distance_m"] == 120.0

    def test_device_reassigned(self, db_session, super_admin, make_device):
        device = make_device()
        emit(db_session, NotificationEvent.DEVICE_REASSIGNED,
             device=device, previous_cluster_id=7)
        rows = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.DEVICE_REASSIGNED
        ).all()
        assert len(rows) == 1
        assert rows[0].payload["previous_cluster_id"] == 7

    def test_user_rejected(self, db_session, make_user):
        user = make_user()
        emit(db_session, NotificationEvent.USER_REJECTED, user=user)
        rows = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.USER_REJECTED
        ).all()
        assert [r.user_id for r in rows] == [user.id]

    def test_role_changed(self, db_session, make_user):
        user = make_user()
        emit(db_session, NotificationEvent.ROLE_CHANGED, user=user, role="ADMIN")
        row = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.ROLE_CHANGED
        ).one()
        assert row.user_id == user.id
        assert row.payload["role"] == "ADMIN"
        # Without an explicit role: falls back to the user's current role.
        emit(db_session, NotificationEvent.ROLE_CHANGED, user=user)
        assert db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.ROLE_CHANGED
        ).count() == 2

    def test_password_reset(self, db_session, make_user):
        user = make_user()
        emit(db_session, NotificationEvent.PASSWORD_RESET, user=user)
        rows = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.PASSWORD_RESET
        ).all()
        assert [r.user_id for r in rows] == [user.id]


class TestServiceEdges:
    def test_send_email_unknown_user(self, service, make_user, make_notification):
        user = make_user()
        n = make_notification(user)
        row = service.notification_repository.get_owned(n.id, user.id)
        assert service.send_email(row, 999999) is False

    def test_deliver_channels_survives_session_factory_failure(self, monkeypatch):
        import app.notification.service as service_module

        def broken_session_factory():
            raise RuntimeError("database exploded")

        monkeypatch.setattr(service_module, "SessionLocal", broken_session_factory)
        # Outer guard: must not raise even when the session cannot be created.
        service_module._deliver_channels([(1, 2, True, False)])


class TestSchedulerErrorLoop:
    def test_broken_schedule_hits_error_path_and_jitter(self, monkeypatch):
        from app.jobs.scheduler import Job, Scheduler
        import app.jobs.scheduler as scheduler_module

        monkeypatch.delenv("NOTIFY_JOBS_ENABLED", raising=False)
        monkeypatch.setattr(scheduler_module, "STARTUP_JITTER_SECONDS", 0.01)
        monkeypatch.setattr(scheduler_module, "LOOP_ERROR_PAUSE_SECONDS", 0.01)
        sched = Scheduler()
        # Job with NO schedule: seconds_until_next_run raises ValueError inside
        # the loop -> logged, paused, loop survives.
        sched.register(Job("broken", lambda: None))
        # Second job exercises the startup-jitter delay branch.
        sched.register(Job("jittered", lambda: None, interval_seconds=60))

        async def main():
            await sched.start()
            await asyncio.sleep(0.1)
            assert all(not task.done() for task in sched._tasks)  # loops alive
            await sched.stop()

        asyncio.run(main())
