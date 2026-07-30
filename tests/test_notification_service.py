"""Unit tests for NotificationService — creation, dedupe, preference gating,
fan-out, per-notification actions, listing/filtering, preferences, cleanup."""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.authentication.enums import UserRole
from app.notification.enums import (
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
)
from app.notification.models import Notification, NotificationPreference


def _send_kwargs(**overrides):
    kwargs = dict(
        title="Hello",
        body="World",
        notification_type=NotificationType.TEST,
        severity=NotificationSeverity.INFO,
        category=NotificationCategory.SYSTEM,
    )
    kwargs.update(overrides)
    return kwargs


# ── send ─────────────────────────────────────────────────────────────────────

class TestSend:
    def test_send_creates_row_with_all_fields(self, service, db_session, make_user,
                                              make_cluster, make_device):
        cluster = make_cluster()
        device = make_device(cluster)
        user = make_user(cluster_id=cluster.id)
        expires = datetime.utcnow() + timedelta(days=1)
        response = service.send(
            user.id,
            **_send_kwargs(
                notification_type=NotificationType.LOW_BATTERY,
                severity=NotificationSeverity.WARNING,
                category=NotificationCategory.SENSOR,
                payload={"voltage": 3.1},
                icon="battery-low",
                action_url=f"/devices/{device.id}",
                cluster_id=cluster.id,
                device_id=device.id,
                dedupe_key=f"low_battery:{device.id}",
                expires_at=expires,
            ),
        )
        assert response is not None
        row = db_session.query(Notification).one()
        assert row.user_id == user.id
        assert row.title == "Hello"
        assert row.body == "World"
        assert row.notification_type == NotificationType.LOW_BATTERY
        assert row.severity == NotificationSeverity.WARNING
        assert row.category == NotificationCategory.SENSOR
        assert row.payload == {"voltage": 3.1}
        assert row.icon == "battery-low"
        assert row.action_url == f"/devices/{device.id}"
        assert row.cluster_id == cluster.id
        assert row.device_id == device.id
        assert row.dedupe_key == f"low_battery:{device.id}"
        assert row.expires_at == expires
        assert row.delivered_at is not None
        assert row.read_at is None and row.archived_at is None and row.deleted_at is None
        # Response mirrors the row (and never exposes user_id).
        assert response.id == row.id
        assert "user_id" not in response.model_dump()

    def test_send_truncates_title_and_body(self, service, make_user, db_session):
        user = make_user()
        service.send(user.id, **_send_kwargs(title="T" * 300, body="B" * 2000))
        row = db_session.query(Notification).one()
        assert len(row.title) == 200
        assert len(row.body) == 1000

    def test_dedupe_suppresses_inside_window(self, service, make_user, db_session):
        user = make_user()
        first = service.send(
            user.id, **_send_kwargs(dedupe_key="k1", dedupe_window_minutes=30)
        )
        second = service.send(
            user.id, **_send_kwargs(dedupe_key="k1", dedupe_window_minutes=30)
        )
        assert first is not None
        assert second is None
        assert db_session.query(Notification).count() == 1

    def test_dedupe_allows_outside_window(self, service, make_user, db_session,
                                          backdate_dedupe_key):
        user = make_user()
        service.send(user.id, **_send_kwargs(dedupe_key="k1", dedupe_window_minutes=30))
        backdate_dedupe_key("k1", minutes=31)
        again = service.send(
            user.id, **_send_kwargs(dedupe_key="k1", dedupe_window_minutes=30)
        )
        assert again is not None
        assert db_session.query(Notification).count() == 2

    def test_dedupe_key_without_window_never_suppresses(self, service, make_user,
                                                        db_session):
        user = make_user()
        service.send(user.id, **_send_kwargs(dedupe_key="k1"))
        service.send(user.id, **_send_kwargs(dedupe_key="k1"))
        assert db_session.query(Notification).count() == 2

    def test_scheduled_future_row_not_dispatched(self, service, make_user, db_session,
                                                 dispatch_recorder):
        user = make_user()
        response = service.send(
            user.id,
            **_send_kwargs(scheduled_for=datetime.utcnow() + timedelta(hours=1)),
        )
        assert response is not None
        row = db_session.query(Notification).one()
        assert row.delivered_at is None
        assert dispatch_recorder == []

    def test_send_dispatches_push_when_enabled(self, service, make_user,
                                               dispatch_recorder):
        user = make_user()
        response = service.send(user.id, **_send_kwargs())
        assert response is not None
        # Default prefs: push on, email off.
        assert dispatch_recorder == [(response.id, user.id, True, False)]

    def test_send_email_flag_only_for_critical_or_summary(self, service, make_user,
                                                          db_session, dispatch_recorder):
        user = make_user()
        service.preference_repository.update_preferences(user.id, email_enabled=True)
        r1 = service.send(user.id, **_send_kwargs(severity=NotificationSeverity.CRITICAL))
        r2 = service.send(
            user.id,
            **_send_kwargs(notification_type=NotificationType.DAILY_SUMMARY),
        )
        r3 = service.send(user.id, **_send_kwargs())  # INFO, non-summary
        flags = {nid: email for (nid, _uid, _push, email) in dispatch_recorder}
        assert flags[r1.id] is True
        assert flags[r2.id] is True
        assert flags[r3.id] is False

    def test_channels_false_creates_row_without_dispatch(self, service, make_user,
                                                         dispatch_recorder, db_session):
        user = make_user()
        service.send(user.id, **_send_kwargs(channels=False))
        assert db_session.query(Notification).count() == 1
        assert dispatch_recorder == []


# ── Preference gating ────────────────────────────────────────────────────────

class TestPreferenceGating:
    def _disable(self, service, user_id, **toggles):
        service.preference_repository.update_preferences(user_id, **toggles)

    def test_in_app_disabled_creates_no_row(self, service, make_user, db_session):
        user = make_user()
        self._disable(service, user.id, in_app_enabled=False)
        assert service.send(user.id, **_send_kwargs()) is None
        assert db_session.query(Notification).count() == 0

    @pytest.mark.parametrize("blocked_type", [
        NotificationType.SPECIES_DETECTED,
        NotificationType.ACTIVITY_SURGE,
    ])
    def test_species_alerts_gate(self, service, make_user, db_session, blocked_type):
        user = make_user()
        self._disable(service, user.id, species_alerts=False)
        assert service.send(
            user.id, **_send_kwargs(notification_type=blocked_type)
        ) is None
        # Unrelated types still pass.
        assert service.send(
            user.id, **_send_kwargs(notification_type=NotificationType.LOW_BATTERY)
        ) is not None

    def test_battery_alerts_gate(self, service, make_user):
        user = make_user()
        self._disable(service, user.id, battery_alerts=False)
        assert service.send(
            user.id, **_send_kwargs(notification_type=NotificationType.LOW_BATTERY)
        ) is None
        assert service.send(
            user.id, **_send_kwargs(notification_type=NotificationType.SPECIES_DETECTED)
        ) is not None

    @pytest.mark.parametrize("blocked_type", [
        NotificationType.DEVICE_OFFLINE,
        NotificationType.DEVICE_ONLINE,
    ])
    def test_offline_alerts_gate(self, service, make_user, blocked_type):
        user = make_user()
        self._disable(service, user.id, offline_alerts=False)
        assert service.send(
            user.id, **_send_kwargs(notification_type=blocked_type)
        ) is None

    @pytest.mark.parametrize("blocked_type", [
        NotificationType.USER_REGISTERED,
        NotificationType.UNKNOWN_DEVICE,
        NotificationType.INVALID_PAYLOAD,
    ])
    def test_admin_alerts_gate(self, service, make_user, blocked_type):
        user = make_user(role=UserRole.SUPER_ADMIN)
        self._disable(service, user.id, admin_alerts=False)
        assert service.send(
            user.id, **_send_kwargs(notification_type=blocked_type)
        ) is None

    @pytest.mark.parametrize("blocked_type", [
        NotificationType.RESEARCHER_REQUEST_SUBMITTED,
        NotificationType.RESEARCHER_REQUEST_APPROVED,
        NotificationType.RESEARCHER_REQUEST_REJECTED,
    ])
    def test_researcher_alerts_gate(self, service, make_user, blocked_type):
        user = make_user()
        self._disable(service, user.id, researcher_alerts=False)
        assert service.send(
            user.id, **_send_kwargs(notification_type=blocked_type)
        ) is None


# ── Fan-out ──────────────────────────────────────────────────────────────────

class TestFanOut:
    def test_send_bulk_creates_one_row_per_recipient(self, service, make_user,
                                                     db_session):
        users = [make_user() for _ in range(3)]
        created = service.send_bulk([u.id for u in users], **_send_kwargs())
        assert created == 3
        rows = db_session.query(Notification).all()
        assert {r.user_id for r in rows} == {u.id for u in users}
        assert all(r.delivered_at is not None for r in rows)

    def test_send_bulk_deduplicates_recipients(self, service, make_user, db_session):
        user = make_user()
        created = service.send_bulk([user.id, user.id, user.id], **_send_kwargs())
        assert created == 1
        assert db_session.query(Notification).count() == 1

    def test_send_bulk_empty_list(self, service):
        assert service.send_bulk([], **_send_kwargs()) == 0

    def test_send_bulk_respects_per_user_preferences(self, service, make_user,
                                                     db_session):
        on = make_user()
        off = make_user()
        service.preference_repository.update_preferences(off.id, in_app_enabled=False)
        created = service.send_bulk([on.id, off.id], **_send_kwargs())
        assert created == 1
        assert db_session.query(Notification).one().user_id == on.id

    def test_send_bulk_dedupe_window(self, service, make_user, db_session):
        users = [make_user() for _ in range(2)]
        ids = [u.id for u in users]
        first = service.send_bulk(
            ids, **_send_kwargs(dedupe_key="bulk", dedupe_window_minutes=60)
        )
        second = service.send_bulk(
            ids, **_send_kwargs(dedupe_key="bulk", dedupe_window_minutes=60)
        )
        assert first == 2
        assert second == 0
        assert db_session.query(Notification).count() == 2

    def test_send_bulk_dispatch_entries(self, service, make_user, dispatch_recorder):
        push_on = make_user()
        push_off = make_user()
        service.preference_repository.update_preferences(push_off.id, push_enabled=False)
        service.send_bulk([push_on.id, push_off.id], **_send_kwargs())
        assert [(uid, push) for (_nid, uid, push, _email) in dispatch_recorder] == [
            (push_on.id, True)
        ]

    def test_send_bulk_scheduled_future_no_dispatch(self, service, make_user,
                                                    db_session, dispatch_recorder):
        user = make_user()
        created = service.send_bulk(
            [user.id],
            **_send_kwargs(scheduled_for=datetime.utcnow() + timedelta(hours=2)),
        )
        assert created == 1
        assert db_session.query(Notification).one().delivered_at is None
        assert dispatch_recorder == []

    def test_notify_cluster_targets_members_and_super_admins(
        self, service, db_session, make_user, make_cluster
    ):
        cluster = make_cluster()
        other = make_cluster()
        member = make_user(cluster_id=cluster.id)
        # Member who is also an ADMIN — included via membership, not the
        # (deprecated) cluster_admins M2M link.
        admin_member = make_user(role=UserRole.ADMIN, cluster_id=cluster.id)
        # M2M-only cluster admin whose membership points elsewhere — the
        # service deliberately excludes them (RBAC_PLAN deprecated the M2M).
        m2m_only_admin = make_user(role=UserRole.ADMIN, cluster_id=other.id)
        cluster.cluster_admins.append(m2m_only_admin)
        db_session.commit()
        super_admin = make_user(role=UserRole.SUPER_ADMIN)
        outsider = make_user(cluster_id=other.id)
        inactive = make_user(cluster_id=cluster.id, is_active=False)

        created = service.notify_cluster(cluster.id, **_send_kwargs())
        assert created == 3
        recipients = {r.user_id for r in db_session.query(Notification).all()}
        assert recipients == {member.id, admin_member.id, super_admin.id}
        assert m2m_only_admin.id not in recipients
        assert outsider.id not in recipients
        assert inactive.id not in recipients
        # cluster_id is stamped on the rows.
        assert {r.cluster_id for r in db_session.query(Notification).all()} == {cluster.id}

    def test_notify_cluster_none_targets_super_admins_only(self, service, db_session,
                                                           make_user, make_cluster):
        cluster = make_cluster()
        make_user(cluster_id=cluster.id)
        super_admin = make_user(role=UserRole.SUPER_ADMIN)
        created = service.notify_cluster(None, **_send_kwargs())
        assert created == 1
        assert db_session.query(Notification).one().user_id == super_admin.id

    def test_notify_admins_targets_active_super_admins_only(self, service, db_session,
                                                            make_user):
        super_admin = make_user(role=UserRole.SUPER_ADMIN)
        make_user(role=UserRole.SUPER_ADMIN, is_active=False)
        make_user(role=UserRole.ADMIN)
        make_user(role=UserRole.USER)
        created = service.notify_admins(**_send_kwargs())
        assert created == 1
        assert db_session.query(Notification).one().user_id == super_admin.id

    def test_broadcast_targets_all_active_users(self, service, db_session, make_user):
        active = [make_user(), make_user(role=UserRole.ADMIN),
                  make_user(role=UserRole.SUPER_ADMIN)]
        make_user(is_active=False)
        created = service.broadcast(**_send_kwargs())
        assert created == 3
        recipients = {r.user_id for r in db_session.query(Notification).all()}
        assert recipients == {u.id for u in active}

    def test_notify_user_aliases_send(self, service, make_user, db_session):
        user = make_user()
        response = service.notify_user(user.id, **_send_kwargs())
        assert response is not None
        assert db_session.query(Notification).one().user_id == user.id


# ── Per-notification actions ─────────────────────────────────────────────────

class TestActions:
    def test_mark_read_and_idempotency(self, service, make_user, make_notification,
                                       db_session):
        user = make_user()
        n = make_notification(user)
        first = service.mark_read(n.id, user.id)
        assert first.read_at is not None
        stamp = first.read_at
        second = service.mark_read(n.id, user.id)
        assert second.read_at == stamp  # unchanged on repeat

    def test_mark_unread_and_idempotency(self, service, make_user, make_notification):
        user = make_user()
        n = make_notification(user, read_at=datetime.utcnow())
        assert service.mark_unread(n.id, user.id).read_at is None
        assert service.mark_unread(n.id, user.id).read_at is None

    def test_cross_user_actions_raise_404(self, service, make_user, make_notification):
        owner = make_user()
        stranger = make_user()
        n = make_notification(owner)
        for action in (service.mark_read, service.mark_unread, service.archive,
                       service.unarchive, service.delete):
            with pytest.raises(HTTPException) as exc:
                action(n.id, stranger.id)
            assert exc.value.status_code == 404

    def test_missing_notification_raises_404(self, service, make_user):
        user = make_user()
        with pytest.raises(HTTPException) as exc:
            service.mark_read(99999, user.id)
        assert exc.value.status_code == 404

    def test_mark_all_read_counts_and_only_touches_unread(self, service, make_user,
                                                          make_notification, db_session):
        user = make_user()
        already_read_at = datetime.utcnow() - timedelta(hours=1)
        make_notification(user, read_at=already_read_at)
        unread = [make_notification(user) for _ in range(3)]
        future = make_notification(
            user, scheduled_for=datetime.utcnow() + timedelta(hours=1), delivered_at=None
        )
        result = service.mark_all_read(user.id)
        assert result.updated == 3
        for n in unread:
            db_session.refresh(n)
            assert n.read_at is not None
        db_session.refresh(future)
        assert future.read_at is None  # scheduled-future rows stay unread
        # Previously-read row untouched.
        read_row = db_session.query(Notification).filter(
            Notification.read_at == already_read_at
        ).first()
        assert read_row is not None
        # Second run: nothing left to update.
        assert service.mark_all_read(user.id).updated == 0

    def test_archive_unarchive(self, service, make_user, make_notification):
        user = make_user()
        n = make_notification(user)
        archived = service.archive(n.id, user.id)
        assert archived.archived_at is not None
        stamp = archived.archived_at
        assert service.archive(n.id, user.id).archived_at == stamp  # idempotent
        assert service.unarchive(n.id, user.id).archived_at is None
        assert service.unarchive(n.id, user.id).archived_at is None

    def test_soft_delete_hides_but_keeps_row(self, service, make_user,
                                             make_notification, db_session):
        user = make_user()
        n = make_notification(user)
        service.delete(n.id, user.id)
        page = service.list_notifications(user.id)
        assert page.total == 0
        row = db_session.query(Notification).filter(Notification.id == n.id).one()
        assert row.deleted_at is not None
        # Acting on a soft-deleted row is a 404.
        with pytest.raises(HTTPException):
            service.mark_read(n.id, user.id)


# ── Unread count / listing ───────────────────────────────────────────────────

class TestListing:
    def test_unread_count(self, service, make_user, make_notification):
        user = make_user()
        make_notification(user)  # unread
        make_notification(user)  # unread
        make_notification(user, read_at=datetime.utcnow())
        make_notification(user, archived_at=datetime.utcnow())  # unread but archived
        make_notification(user, deleted_at=datetime.utcnow())
        make_notification(user, scheduled_for=datetime.utcnow() + timedelta(hours=1))
        make_notification(user, expires_at=datetime.utcnow() - timedelta(hours=1))
        assert service.unread_count(user.id).count == 2

    def test_list_default_excludes_archived_deleted_scheduled_expired(
        self, service, make_user, make_notification
    ):
        user = make_user()
        visible = make_notification(user)
        make_notification(user, archived_at=datetime.utcnow())
        make_notification(user, deleted_at=datetime.utcnow())
        make_notification(user, scheduled_for=datetime.utcnow() + timedelta(hours=1))
        make_notification(user, expires_at=datetime.utcnow() - timedelta(minutes=1))
        page = service.list_notifications(user.id)
        assert page.total == 1
        assert page.items[0].id == visible.id

    def test_scheduled_becomes_visible_when_due(self, service, make_user,
                                                make_notification, db_session):
        user = make_user()
        n = make_notification(
            user, scheduled_for=datetime.utcnow() + timedelta(hours=1)
        )
        assert service.list_notifications(user.id).total == 0
        db_session.query(Notification).filter(Notification.id == n.id).update(
            {"scheduled_for": datetime.utcnow() - timedelta(minutes=1)}
        )
        db_session.commit()
        page = service.list_notifications(user.id)
        assert page.total == 1
        assert page.items[0].id == n.id

    def test_unread_only_filter(self, service, make_user, make_notification):
        user = make_user()
        unread = make_notification(user)
        make_notification(user, read_at=datetime.utcnow())
        page = service.list_notifications(user.id, unread_only=True)
        assert [n.id for n in page.items] == [unread.id]

    def test_category_severity_type_filters(self, service, make_user, make_notification):
        user = make_user()
        target = make_notification(
            user,
            category=NotificationCategory.SENSOR,
            severity=NotificationSeverity.CRITICAL,
            notification_type=NotificationType.LOW_BATTERY,
        )
        make_notification(user)  # SYSTEM / INFO / TEST
        assert [n.id for n in service.list_notifications(
            user.id, category=NotificationCategory.SENSOR).items] == [target.id]
        assert [n.id for n in service.list_notifications(
            user.id, severity=NotificationSeverity.CRITICAL).items] == [target.id]
        assert [n.id for n in service.list_notifications(
            user.id, notification_type=NotificationType.LOW_BATTERY).items] == [target.id]

    def test_archived_filter_shows_only_archived(self, service, make_user,
                                                 make_notification):
        user = make_user()
        make_notification(user)
        archived = make_notification(user, archived_at=datetime.utcnow())
        page = service.list_notifications(user.id, archived=True)
        assert [n.id for n in page.items] == [archived.id]

    def test_search_matches_title_and_body_case_insensitive(self, service, make_user,
                                                            make_notification):
        user = make_user()
        by_title = make_notification(user, title="Battery LOW on unit 7", body="x")
        by_body = make_notification(user, title="x", body="the battery died")
        make_notification(user, title="unrelated", body="unrelated")
        page = service.list_notifications(user.id, search="BaTTeRy")
        assert {n.id for n in page.items} == {by_title.id, by_body.id}

    def test_sort_newest_and_oldest(self, service, make_user, make_notification,
                                    backdate):
        user = make_user()
        old = make_notification(user)
        backdate(old, minutes=60)
        new = make_notification(user)
        newest = service.list_notifications(user.id, sort="newest")
        assert [n.id for n in newest.items] == [new.id, old.id]
        oldest = service.list_notifications(user.id, sort="oldest")
        assert [n.id for n in oldest.items] == [old.id, new.id]

    def test_pagination_envelope(self, service, make_user, make_notification):
        user = make_user()
        for _ in range(25):
            make_notification(user)
        page = service.list_notifications(user.id, page=1, page_size=10)
        assert page.total == 25
        assert page.page == 1
        assert page.page_size == 10
        assert page.total_pages == 3
        assert len(page.items) == 10
        last = service.list_notifications(user.id, page=3, page_size=10)
        assert len(last.items) == 5


# ── Cleanup ──────────────────────────────────────────────────────────────────

class TestCleanup:
    def test_delete_expired_purges_expired_and_old_soft_deleted(
        self, service, make_user, make_notification, db_session
    ):
        user = make_user()
        make_notification(user, expires_at=datetime.utcnow() - timedelta(minutes=1))
        make_notification(user, expires_at=datetime.utcnow() + timedelta(days=1))
        make_notification(user, deleted_at=datetime.utcnow() - timedelta(days=31))
        recent_deleted = make_notification(
            user, deleted_at=datetime.utcnow() - timedelta(days=5)
        )
        keep = make_notification(user)
        removed = service.delete_expired()
        assert removed == 2
        remaining = {n.id for n in db_session.query(Notification).all()}
        assert keep.id in remaining
        assert recent_deleted.id in remaining  # under 30d — still retained
        assert len(remaining) == 3

    def test_delete_expired_noop(self, service, make_user, make_notification):
        user = make_user()
        make_notification(user)
        assert service.delete_expired() == 0


# ── Preferences ──────────────────────────────────────────────────────────────

class TestPreferences:
    def test_get_or_create_defaults(self, service, make_user, db_session):
        user = make_user()
        prefs = service.get_preferences(user.id)
        assert prefs.email_enabled is False
        for field in ("species_alerts", "battery_alerts", "offline_alerts",
                      "admin_alerts", "researcher_alerts", "push_enabled",
                      "in_app_enabled"):
            assert getattr(prefs, field) is True, field
        # Row was lazily created exactly once.
        assert db_session.query(NotificationPreference).count() == 1
        service.get_preferences(user.id)
        assert db_session.query(NotificationPreference).count() == 1

    def test_partial_update_only_touches_provided_fields(self, service, make_user):
        from app.notification.schema import NotificationPreferenceUpdate

        user = make_user()
        updated = service.update_preferences(
            user.id, NotificationPreferenceUpdate(species_alerts=False,
                                                  email_enabled=True)
        )
        assert updated.species_alerts is False
        assert updated.email_enabled is True
        assert updated.battery_alerts is True  # untouched
        assert updated.push_enabled is True

    def test_send_test_creates_and_raises_when_suppressed(self, service, make_user):
        user = make_user(role=UserRole.SUPER_ADMIN)
        response = service.send_test(user.id)
        assert response.notification_type == NotificationType.TEST
        service.preference_repository.update_preferences(user.id, in_app_enabled=False)
        with pytest.raises(HTTPException) as exc:
            service.send_test(user.id)
        assert exc.value.status_code == 400
