"""Background jobs: offline detection state machine, cleanup, push retry,
summaries, health sweep, and the asyncio scheduler itself."""
import asyncio
from datetime import datetime, time, timedelta

import pytest

import app.jobs.cleanup as cleanup_module
import app.jobs.health as health_module
import app.jobs.offline_detection as offline_module
import app.jobs.push_retry as push_retry_module
import app.jobs.scheduler as scheduler_module
import app.jobs.summaries as summaries_module
from app.authentication.enums import UserRole
from app.device.models import Device, MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading
from app.jobs.offline_detection import run_offline_detection
from app.jobs.scheduler import Job, Scheduler, register_all_jobs
from app.notification.enums import DeliveryChannel, DeliveryStatus, NotificationType
from app.notification.models import (
    Notification,
    NotificationDelivery,
    PushSubscription,
)


@pytest.fixture
def super_admin(make_user):
    return make_user(role=UserRole.SUPER_ADMIN)


@pytest.fixture
def patch_job_sessions(monkeypatch, TestingSessionLocal):
    """Point every job module's SessionLocal at the test engine."""
    for module in (offline_module, cleanup_module, push_retry_module,
                   summaries_module, health_module):
        monkeypatch.setattr(module, "SessionLocal", TestingSessionLocal)


def _types(db_session, type_):
    return db_session.query(Notification).filter(
        Notification.notification_type == type_
    ).all()


# ── Offline detection ────────────────────────────────────────────────────────

class TestOfflineDetection:
    def test_stale_device_marked_offline_once(self, db_session, super_admin,
                                              make_device, patch_job_sessions):
        device = make_device(last_activity=datetime.utcnow() - timedelta(hours=2))
        run_offline_detection()
        db_session.expire_all()
        device = db_session.get(Device, device.id)
        assert device.offline_since is not None
        assert len(_types(db_session, NotificationType.DEVICE_OFFLINE)) == 1
        # Second sweep while still offline: NO re-alert (state machine).
        run_offline_detection()
        assert len(_types(db_session, NotificationType.DEVICE_OFFLINE)) == 1

    def test_fresh_device_untouched(self, db_session, super_admin, make_device,
                                    patch_job_sessions):
        device = make_device(last_activity=datetime.utcnow())
        run_offline_detection()
        db_session.expire_all()
        assert db_session.get(Device, device.id).offline_since is None
        assert _types(db_session, NotificationType.DEVICE_OFFLINE) == []

    def test_recovery_clears_state_and_notifies_online(self, db_session, super_admin,
                                                       make_device, patch_job_sessions):
        device = make_device(
            last_activity=datetime.utcnow(),
            offline_since=datetime.utcnow() - timedelta(hours=1),
        )
        run_offline_detection()
        db_session.expire_all()
        assert db_session.get(Device, device.id).offline_since is None
        assert len(_types(db_session, NotificationType.DEVICE_ONLINE)) == 1
        # Sweep again: no repeat DEVICE_ONLINE.
        run_offline_detection()
        assert len(_types(db_session, NotificationType.DEVICE_ONLINE)) == 1

    def test_full_outage_cycle(self, db_session, super_admin, make_device,
                               patch_job_sessions):
        device = make_device(last_activity=datetime.utcnow() - timedelta(hours=2))
        run_offline_detection()  # -> offline
        # Device comes back.
        db_session.query(Device).filter(Device.id == device.id).update(
            {"last_activity": datetime.utcnow()}
        )
        db_session.commit()
        run_offline_detection()  # -> online
        db_session.expire_all()
        assert db_session.get(Device, device.id).offline_since is None
        assert len(_types(db_session, NotificationType.DEVICE_OFFLINE)) == 1
        assert len(_types(db_session, NotificationType.DEVICE_ONLINE)) == 1


# ── Cleanup job ──────────────────────────────────────────────────────────────

class TestCleanupJob:
    def test_deletes_expired_and_old_soft_deleted(self, db_session, make_user,
                                                  make_notification,
                                                  patch_job_sessions):
        user = make_user()
        make_notification(user, expires_at=datetime.utcnow() - timedelta(minutes=1))
        make_notification(user, deleted_at=datetime.utcnow() - timedelta(days=31))
        keep = make_notification(user)
        cleanup_module.run_notification_cleanup()
        db_session.expire_all()
        remaining = db_session.query(Notification).all()
        assert [n.id for n in remaining] == [keep.id]


# ── Push retry job ───────────────────────────────────────────────────────────

class FakePushChannel:
    """Stands in for PushChannel; outcome controlled per-test."""
    outcome = True
    calls: list = []

    def __init__(self, session):
        self.session = session

    def send(self, notification, user):
        FakePushChannel.calls.append((notification.id, user.id))
        return FakePushChannel.outcome


@pytest.fixture
def fake_channel(monkeypatch):
    FakePushChannel.calls = []
    FakePushChannel.outcome = True
    monkeypatch.setattr(push_retry_module, "PushChannel", FakePushChannel)
    return FakePushChannel


def _make_failed_delivery(db_session, notification, *, attempts=1,
                          last_attempt_minutes_ago=60,
                          channel=DeliveryChannel.PUSH):
    delivery = NotificationDelivery(
        notification_id=notification.id,
        channel=channel,
        status=DeliveryStatus.FAILED,
        attempts=attempts,
        last_attempt_at=datetime.utcnow() - timedelta(minutes=last_attempt_minutes_ago),
    )
    db_session.add(delivery)
    db_session.commit()
    return delivery


def _add_subscription(db_session, user, active=True):
    subscription = PushSubscription(
        user_id=user.id,
        endpoint=f"https://push.example.com/{user.id}-{active}",
        p256dh="k", auth="a", active=active,
    )
    db_session.add(subscription)
    db_session.commit()
    return subscription


class TestPushRetry:
    def test_due_delivery_retried_success(self, db_session, make_user,
                                          make_notification, patch_job_sessions,
                                          fake_channel):
        user = make_user()
        n = make_notification(user)
        _add_subscription(db_session, user)
        delivery = _make_failed_delivery(db_session, n, attempts=1,
                                         last_attempt_minutes_ago=60)
        push_retry_module.run_push_retry()
        db_session.expire_all()
        delivery = db_session.get(NotificationDelivery, delivery.id)
        assert fake_channel.calls == [(n.id, user.id)]
        assert delivery.status == DeliveryStatus.SENT
        assert delivery.attempts == 2
        assert delivery.error is None

    def test_failed_retry_increments_attempts(self, db_session, make_user,
                                              make_notification, patch_job_sessions,
                                              fake_channel):
        user = make_user()
        n = make_notification(user)
        _add_subscription(db_session, user)
        delivery = _make_failed_delivery(db_session, n, attempts=2,
                                         last_attempt_minutes_ago=600)
        fake_channel.outcome = False
        push_retry_module.run_push_retry()
        db_session.expire_all()
        delivery = db_session.get(NotificationDelivery, delivery.id)
        assert delivery.status == DeliveryStatus.FAILED
        assert delivery.attempts == 3
        assert delivery.error == "push retry failed"

    def test_backoff_skips_recent_failure(self, db_session, make_user,
                                          make_notification, patch_job_sessions,
                                          fake_channel):
        # attempts=2 -> backoff 2 * 600s = 20 min; last attempt 5 min ago -> skip.
        user = make_user()
        n = make_notification(user)
        _add_subscription(db_session, user)
        delivery = _make_failed_delivery(db_session, n, attempts=2,
                                         last_attempt_minutes_ago=5)
        push_retry_module.run_push_retry()
        db_session.expire_all()
        delivery = db_session.get(NotificationDelivery, delivery.id)
        assert fake_channel.calls == []
        assert delivery.attempts == 2  # untouched

    def test_attempt_cap_excludes_delivery(self, db_session, make_user,
                                           make_notification, patch_job_sessions,
                                           fake_channel):
        user = make_user()
        n = make_notification(user)
        _add_subscription(db_session, user)
        _make_failed_delivery(db_session, n, attempts=5, last_attempt_minutes_ago=600)
        push_retry_module.run_push_retry()
        assert fake_channel.calls == []

    def test_email_channel_not_selected(self, db_session, make_user,
                                        make_notification, patch_job_sessions,
                                        fake_channel):
        user = make_user()
        n = make_notification(user)
        _add_subscription(db_session, user)
        _make_failed_delivery(db_session, n, channel=DeliveryChannel.EMAIL,
                              last_attempt_minutes_ago=600)
        push_retry_module.run_push_retry()
        assert fake_channel.calls == []

    def test_soft_deleted_notification_ages_out(self, db_session, make_user,
                                                make_notification, patch_job_sessions,
                                                fake_channel):
        user = make_user()
        n = make_notification(user, deleted_at=datetime.utcnow())
        delivery = _make_failed_delivery(db_session, n, last_attempt_minutes_ago=600)
        push_retry_module.run_push_retry()
        db_session.expire_all()
        delivery = db_session.get(NotificationDelivery, delivery.id)
        assert fake_channel.calls == []
        assert delivery.attempts == 2
        assert delivery.error == "notification no longer exists"

    def test_missing_user_ages_out(self, db_session, make_user, make_notification,
                                   patch_job_sessions, fake_channel):
        user = make_user()
        n = make_notification(user)
        db_session.query(Notification).filter(Notification.id == n.id).update(
            {"user_id": 999999}
        )
        db_session.commit()
        delivery = _make_failed_delivery(db_session, n, last_attempt_minutes_ago=600)
        push_retry_module.run_push_retry()
        db_session.expire_all()
        assert db_session.get(NotificationDelivery, delivery.id).error == (
            "recipient no longer exists"
        )

    def test_push_disabled_ages_out(self, db_session, make_user, make_notification,
                                    patch_job_sessions, fake_channel, service):
        user = make_user()
        service.preference_repository.update_preferences(user.id, push_enabled=False)
        n = make_notification(user)
        _add_subscription(db_session, user)
        delivery = _make_failed_delivery(db_session, n, last_attempt_minutes_ago=600)
        push_retry_module.run_push_retry()
        db_session.expire_all()
        assert db_session.get(NotificationDelivery, delivery.id).error == (
            "push disabled in preferences"
        )
        assert fake_channel.calls == []

    def test_no_active_subscription_ages_out(self, db_session, make_user,
                                             make_notification, patch_job_sessions,
                                             fake_channel):
        user = make_user()
        n = make_notification(user)
        _add_subscription(db_session, user, active=False)
        delivery = _make_failed_delivery(db_session, n, last_attempt_minutes_ago=600)
        push_retry_module.run_push_retry()
        db_session.expire_all()
        assert db_session.get(NotificationDelivery, delivery.id).error == (
            "no active push subscriptions"
        )
        assert fake_channel.calls == []


# ── Summaries ────────────────────────────────────────────────────────────────

class TestSummaries:
    @pytest.fixture
    def seeded_world(self, db_session, make_user, make_cluster, make_device):
        cluster = make_cluster()
        empty_cluster = make_cluster()
        member = make_user(cluster_id=cluster.id)
        empty_member = make_user(cluster_id=empty_cluster.id)
        super_admin = make_user(role=UserRole.SUPER_ADMIN)
        device = make_device(cluster)
        now = datetime.utcnow()
        # Two mosquito events in the last 24h, one with a species reading.
        for index in range(2):
            event = MosquitoEvent(device_id=device.id,
                                  timestamp=now - timedelta(hours=1), count=1)
            db_session.add(event)
            db_session.flush()
            if index == 0:
                db_session.add(MosquitoIndividualReading(
                    batch_id=event.id, detection_timestamp=now,
                    species="Anopheles gambiae", genus="Anopheles",
                    age_group="adult", sex="female",
                ))
        # Low-battery latest reading.
        db_session.add(SensorDeviceReading(
            device_id=device.id, timestamp=now, battery_voltage=3.0,
        ))
        db_session.commit()
        return dict(cluster=cluster, empty_cluster=empty_cluster, member=member,
                    empty_member=empty_member, super_admin=super_admin, device=device)

    def test_daily_summary_aggregates(self, db_session, seeded_world,
                                      patch_job_sessions):
        summaries_module.run_daily_summary()
        world = seeded_world
        member_rows = [n for n in _types(db_session, NotificationType.DAILY_SUMMARY)
                       if n.user_id == world["member"].id]
        assert len(member_rows) == 1
        stats = member_rows[0].payload
        assert stats["cluster_id"] == world["cluster"].id
        assert stats["mosquito_events"] == 2
        assert stats["mosquito_total"] == 2
        assert stats["top_species"] == "Anopheles gambiae"
        assert stats["device_count"] == 1
        assert stats["low_battery_devices"] == 1
        assert stats["offline_devices"] == 0
        assert "2 detection event(s)" in member_rows[0].body

    def test_empty_cluster_skipped(self, db_session, seeded_world, patch_job_sessions):
        summaries_module.run_daily_summary()
        world = seeded_world
        empty_rows = [n for n in _types(db_session, NotificationType.DAILY_SUMMARY)
                      if n.user_id == world["empty_member"].id]
        assert empty_rows == []

    def test_super_admin_gets_single_global_summary(self, db_session, seeded_world,
                                                    patch_job_sessions):
        summaries_module.run_daily_summary()
        world = seeded_world
        super_rows = [n for n in _types(db_session, NotificationType.DAILY_SUMMARY)
                      if n.user_id == world["super_admin"].id]
        assert len(super_rows) == 1  # one global, not one per cluster
        assert super_rows[0].payload["scope"] == "global"
        assert super_rows[0].payload["cluster_id"] is None

    def test_weekly_summary_type(self, db_session, seeded_world, patch_job_sessions):
        summaries_module.run_weekly_summary()
        rows = _types(db_session, NotificationType.WEEKLY_SUMMARY)
        assert rows  # member + super admin
        assert all(n.payload["period"] == "7 days" for n in rows)

    def test_nothing_emitted_on_empty_database(self, db_session, patch_job_sessions):
        summaries_module.run_daily_summary()
        assert db_session.query(Notification).count() == 0


# ── Health sweep ─────────────────────────────────────────────────────────────

class TestHealthSweep:
    def test_maintenance_due_for_30d_inactive(self, db_session, super_admin,
                                              make_device, patch_job_sessions):
        make_device(last_activity=datetime.utcnow() - timedelta(days=31))
        make_device(last_activity=datetime.utcnow())  # healthy
        health_module.run_health_sweep()
        rows = _types(db_session, NotificationType.MAINTENANCE_DUE)
        assert len(rows) == 1
        assert rows[0].payload["days_inactive"] == 31

    def test_stuck_sensor_detected(self, db_session, super_admin, make_device,
                                   patch_job_sessions):
        device = make_device(last_activity=datetime.utcnow())
        now = datetime.utcnow()
        for index in range(5):
            db_session.add(SensorDeviceReading(
                device_id=device.id, timestamp=now - timedelta(minutes=index),
                external_temperature=25.0, internal_temperature=26.0,
                external_humidity=70.0, internal_humidity=60.0,
                battery_voltage=3.8,
            ))
        db_session.commit()
        health_module.run_health_sweep()
        assert len(_types(db_session, NotificationType.SENSOR_MALFUNCTION)) == 1

    def test_varying_readings_not_stuck(self, db_session, super_admin, make_device,
                                        patch_job_sessions):
        device = make_device(last_activity=datetime.utcnow())
        now = datetime.utcnow()
        for index in range(5):
            db_session.add(SensorDeviceReading(
                device_id=device.id, timestamp=now - timedelta(minutes=index),
                external_temperature=25.0 + index, battery_voltage=3.8,
            ))
        db_session.commit()
        health_module.run_health_sweep()
        assert _types(db_session, NotificationType.SENSOR_MALFUNCTION) == []

    def test_all_none_readings_not_stuck(self, db_session, super_admin, make_device,
                                         patch_job_sessions):
        device = make_device(last_activity=datetime.utcnow())
        now = datetime.utcnow()
        for index in range(5):
            db_session.add(SensorDeviceReading(
                device_id=device.id, timestamp=now - timedelta(minutes=index),
            ))
        db_session.commit()
        health_module.run_health_sweep()
        assert _types(db_session, NotificationType.SENSOR_MALFUNCTION) == []

    def test_too_few_readings_not_stuck(self, db_session, super_admin, make_device,
                                        patch_job_sessions):
        device = make_device(last_activity=datetime.utcnow())
        for index in range(3):
            db_session.add(SensorDeviceReading(
                device_id=device.id, timestamp=datetime.utcnow(),
                external_temperature=25.0, battery_voltage=3.8,
            ))
        db_session.commit()
        health_module.run_health_sweep()
        assert _types(db_session, NotificationType.SENSOR_MALFUNCTION) == []


# ── Scheduler ────────────────────────────────────────────────────────────────

class TestJobNextRunMath:
    def test_interval(self):
        job = Job("j", lambda: None, interval_seconds=120)
        assert job.seconds_until_next_run(datetime(2026, 7, 30, 12, 0)) == 120.0

    def test_daily_before_time(self):
        job = Job("j", lambda: None, daily_at=time(7, 0))
        now = datetime(2026, 7, 30, 6, 0)
        assert job.seconds_until_next_run(now) == 3600.0

    def test_daily_after_time_rolls_to_tomorrow(self):
        job = Job("j", lambda: None, daily_at=time(7, 0))
        now = datetime(2026, 7, 30, 8, 0)
        assert job.seconds_until_next_run(now) == 23 * 3600.0

    def test_daily_exactly_at_time_rolls_to_tomorrow(self):
        job = Job("j", lambda: None, daily_at=time(7, 0))
        now = datetime(2026, 7, 30, 7, 0)
        assert job.seconds_until_next_run(now) == 24 * 3600.0

    def test_weekly_later_this_week(self):
        # 2026-07-30 is a Thursday (weekday 3); target Friday (4) 07:00.
        job = Job("j", lambda: None, weekly_at=(4, time(7, 0)))
        now = datetime(2026, 7, 30, 6, 0)
        assert job.seconds_until_next_run(now) == (24 + 1) * 3600.0

    def test_weekly_wraps_to_next_week(self):
        # Target Monday (0) from Thursday 08:00 -> 4 days minus 1 hour.
        job = Job("j", lambda: None, weekly_at=(0, time(7, 0)))
        now = datetime(2026, 7, 30, 8, 0)
        assert job.seconds_until_next_run(now) == (4 * 24 - 1) * 3600.0

    def test_weekly_same_day_after_time_next_week(self):
        job = Job("j", lambda: None, weekly_at=(3, time(7, 0)))  # Thursday
        now = datetime(2026, 7, 30, 8, 0)  # Thursday 08:00
        assert job.seconds_until_next_run(now) == (7 * 24 - 1) * 3600.0

    def test_no_schedule_raises(self):
        with pytest.raises(ValueError):
            Job("j", lambda: None).seconds_until_next_run()


class TestSchedulerLifecycle:
    def test_register_all_jobs_idempotent(self, monkeypatch):
        fresh = Scheduler()
        monkeypatch.setattr(scheduler_module, "scheduler", fresh)
        register_all_jobs()
        first = [job.name for job in fresh.jobs]
        register_all_jobs()
        assert [job.name for job in fresh.jobs] == first
        assert sorted(first) == sorted([
            "offline-detection", "notification-cleanup", "push-retry",
            "daily-summary", "weekly-summary", "device-health",
        ])

    def test_start_runs_job_and_stop_cancels(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_JOBS_ENABLED", raising=False)
        runs = []
        sched = Scheduler()
        sched.register(Job("tick", lambda: runs.append(1), interval_seconds=0.01))

        async def main():
            await sched.start()
            assert len(sched._tasks) == 1
            await sched.start()  # second start is a no-op
            assert len(sched._tasks) == 1
            await asyncio.sleep(0.2)
            await sched.stop()
            assert sched._tasks == []

        asyncio.run(main())
        assert runs, "the 10ms job never ran"

    def test_jobs_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_JOBS_ENABLED", "0")
        sched = Scheduler()
        sched.register(Job("tick", lambda: None, interval_seconds=0.01))

        async def main():
            await sched.start()
            assert sched._tasks == []

        asyncio.run(main())

    def test_job_failure_does_not_propagate(self):
        def boom():
            raise RuntimeError("job blew up")

        # _execute must swallow the failure.
        Scheduler._execute(Job("boom", boom, interval_seconds=1))

    def test_parse_utc_time(self):
        assert scheduler_module._parse_utc_time("09:30", time(7, 0)) == time(9, 30)
        assert scheduler_module._parse_utc_time("bogus", time(7, 0)) == time(7, 0)
