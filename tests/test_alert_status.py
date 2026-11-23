"""Alert status opened -> acknowledged -> closed, shared per alert_group,
with response times read straight off the stamps (never backfilled)."""
from datetime import datetime, timedelta

import app.jobs.summaries as summaries_module
from app.audit.models import AuditAction, AuditLog
from app.authentication.enums import UserRole
from app.notification.enums import NotificationCategory, NotificationSeverity, NotificationType
from app.notification.models import Notification

KW = dict(title="t", body="b", notification_type=NotificationType.LOW_BATTERY,
          severity=NotificationSeverity.WARNING, category=NotificationCategory.SENSOR)


def _rows(db_session, user):
    return (db_session.query(Notification).filter(Notification.user_id == user.id)
            .order_by(Notification.id).all())


def test_json_keys_and_opened_default(client, login_as, service, db_session, make_user):
    user = make_user()
    service.send(user.id, **KW)
    login_as(user)
    item = client.get("/notifications").json()["items"][0]
    assert {"status", "acknowledged_at", "acknowledged_by_name",
            "time_to_acknowledge_s", "time_to_close_s"} <= set(item)
    assert item["status"] == "opened"
    assert item["acknowledged_at"] is None and item["time_to_acknowledge_s"] is None
    assert item["time_to_close_s"] is None


def test_informational_row_has_no_status(client, login_as, make_user, make_notification):
    user = make_user()
    row = make_notification(user)  # TEST type
    login_as(user)
    assert client.get("/notifications").json()["items"][0]["status"] is None
    assert client.patch(f"/notifications/{row.id}/acknowledged").status_code == 400


def test_full_flow_shared_across_group_with_response_times(client, login_as, service,
                                                           db_session, make_user):
    alice = make_user(first_name="Alice", last_name="Ade")
    bob = make_user(first_name="Bob", last_name="Bee")
    service.send_bulk([alice.id, bob.id], **KW)
    service.send(alice.id, **KW)  # separate emission: must stay opened
    a_row, a_other = _rows(db_session, alice)
    b_row = _rows(db_session, bob)[0]
    # Pin created_at so the response times are exact.
    created = datetime.utcnow() - timedelta(minutes=10)
    db_session.query(Notification).filter(
        Notification.alert_group == a_row.alert_group).update({"created_at": created})
    db_session.commit()

    login_as(alice)
    r = client.patch(f"/notifications/{a_row.id}/acknowledged").json()
    assert r["status"] == "acknowledged" and r["acknowledged_by_name"] == "Alice Ade"
    assert 590 <= r["time_to_acknowledge_s"] <= 610 and r["time_to_close_s"] is None

    login_as(bob)
    # Bob's second acknowledge does not overwrite Alice.
    r = client.patch(f"/notifications/{b_row.id}/acknowledged").json()
    assert r["acknowledged_by_name"] == "Alice Ade"
    r = client.patch(f"/notifications/{b_row.id}/handled").json()
    assert r["status"] == "closed" and r["handled_by_name"] == "Bob Bee"
    assert r["time_to_close_s"] >= r["time_to_acknowledge_s"]

    login_as(alice)
    items = {i["id"]: i for i in client.get("/notifications").json()["items"]}
    assert items[a_row.id]["status"] == "closed"
    assert items[a_row.id]["handled_by_name"] == "Bob Bee"
    assert items[a_other.id]["status"] == "opened"

    # Closed alerts can't be (un)acknowledged until reopened.
    assert client.patch(f"/notifications/{a_row.id}/unacknowledged").status_code == 409
    assert client.patch(f"/notifications/{a_row.id}/unhandled").json()["status"] == "acknowledged"
    r = client.patch(f"/notifications/{a_row.id}/unacknowledged").json()
    assert r["status"] == "opened" and r["acknowledged_at"] is None
    db_session.expire_all()
    assert db_session.get(Notification, b_row.id).acknowledged_at is None

    actions = [a.action for a in db_session.query(AuditLog).all()]
    assert actions.count(AuditAction.ALERT_ACKNOWLEDGED) == 1  # bob's no-op not audited
    assert actions.count(AuditAction.ALERT_UNACKNOWLEDGED) == 1


def test_close_without_acknowledge_leaves_ack_null(client, login_as, service, db_session,
                                                   make_user):
    user = make_user()
    service.send(user.id, **KW)
    row = _rows(db_session, user)[0]
    login_as(user)
    r = client.patch(f"/notifications/{row.id}/handled").json()
    assert r["status"] == "closed"
    assert r["acknowledged_at"] is None and r["acknowledged_by_name"] is None
    assert r["time_to_acknowledge_s"] is None and r["time_to_close_s"] is not None


def test_close_racing_acknowledge_wins(service, db_session, make_user, monkeypatch):
    """A close committed after the alert was loaded (i.e. after any pre-check)
    must stop the acknowledge UPDATE and surface as 409."""
    import pytest
    from fastapi import HTTPException

    alice, bob = make_user(), make_user()
    service.send_bulk([alice.id, bob.id], **KW)
    a_row, b_row = _rows(db_session, alice)[0], _rows(db_session, bob)[0]
    repo = service.notification_repository
    real_get = repo.get_owned

    def get_then_bob_closes(notification_id, user_id):
        row = real_get(notification_id, user_id)
        db_session.query(Notification).filter(
            Notification.alert_group == row.alert_group
        ).update({"handled_at": datetime.utcnow(), "handled_by_user_id": bob.id},
                 synchronize_session=False)
        db_session.commit()
        return row

    monkeypatch.setattr(repo, "get_owned", get_then_bob_closes)
    with pytest.raises(HTTPException) as exc:
        service.set_acknowledged(a_row.id, alice.id, True)
    assert exc.value.status_code == 409
    db_session.expire_all()
    assert db_session.get(Notification, a_row.id).acknowledged_at is None
    assert db_session.get(Notification, b_row.id).acknowledged_at is None


def test_non_recipient_gets_404(client, login_as, service, db_session, make_user):
    owner, outsider = make_user(), make_user()
    service.send(owner.id, **KW)
    row = _rows(db_session, owner)[0]
    login_as(outsider)
    assert client.patch(f"/notifications/{row.id}/acknowledged").status_code == 404
    assert client.patch(f"/notifications/{row.id}/unacknowledged").status_code == 404
    db_session.expire_all()
    assert db_session.get(Notification, row.id).acknowledged_at is None


def test_status_filter(client, login_as, service, db_session, make_user, make_notification):
    user = make_user()
    for _ in range(3):
        service.send(user.id, **KW)
    make_notification(user)  # informational: in no status bucket
    opened, acked, closed = _rows(db_session, user)[:3]
    login_as(user)
    client.patch(f"/notifications/{acked.id}/acknowledged")
    client.patch(f"/notifications/{closed.id}/handled")

    def ids(q):
        return [i["id"] for i in client.get(f"/notifications?{q}").json()["items"]]

    assert ids("status=opened") == [opened.id]
    assert ids("status=acknowledged") == [acked.id]
    assert ids("status=closed") == [closed.id]
    assert sorted(ids("handled=false")) == sorted([opened.id, acked.id])  # old filter intact
    assert client.get("/notifications?status=bogus").status_code == 422


def test_daily_summary_counts_alerts_and_device_status(db_session, monkeypatch,
                                                       TestingSessionLocal, service,
                                                       make_user, make_cluster, make_device):
    monkeypatch.setattr(summaries_module, "SessionLocal", TestingSessionLocal)
    cluster = make_cluster()
    member = make_user(cluster_id=cluster.id)
    other = make_user(cluster_id=cluster.id)
    make_user(role=UserRole.SUPER_ADMIN)
    make_device(cluster)  # reporting now
    make_device(cluster, last_sensor_data_at=None)  # never sent telemetry: silent
    for _ in range(6):  # 6 emissions, each to 2 members + super admin
        service.notify_cluster(cluster.id, **KW)
    first, second, _, fourth, gone, sixth = _rows(db_session, member)
    service.set_acknowledged(first.id, member.id, True)
    service.set_acknowledged(sixth.id, member.id, True)  # acknowledged, still open
    service.set_handled(second.id, member.id, True)
    service.set_acknowledged(fourth.id, member.id, True)  # acknowledged THEN closed
    service.set_handled(fourth.id, member.id, True)
    # An open alert every recipient deleted: no live row, must not count.
    db_session.query(Notification).filter(
        Notification.alert_group == gone.alert_group
    ).update({"deleted_at": datetime.utcnow()}, synchronize_session=False)
    db_session.commit()

    summaries_module.run_daily_summary()
    row = (db_session.query(Notification)
           .filter(Notification.user_id == other.id,
                   Notification.notification_type == NotificationType.DAILY_SUMMARY).one())
    stats = row.payload
    assert stats["reporting_devices"] == 1 and stats["silent_devices"] == 1
    assert stats["alerts_opened"] == 5  # per live emission, not per recipient
    assert stats["alerts_acknowledged"] == 3
    assert stats["alerts_closed"] == 2
    # Not closed: first + sixth (acknowledged) + third. Counting "not
    # acknowledged" instead would give second + third = 2.
    assert stats["alerts_still_open"] == 3
    assert "1 reporting, 1 silent" in row.body and "3 still open" in row.body


def test_cluster_with_only_open_alerts_still_gets_summary(db_session, monkeypatch,
                                                          TestingSessionLocal, service,
                                                          make_user, make_cluster):
    monkeypatch.setattr(summaries_module, "SessionLocal", TestingSessionLocal)
    cluster = make_cluster()  # no devices, no detections
    member = make_user(cluster_id=cluster.id)
    service.notify_cluster(cluster.id, **KW)
    old = datetime.utcnow() - timedelta(days=3)  # outside the 24h window
    db_session.query(Notification).update({"created_at": old})
    db_session.commit()

    summaries_module.run_daily_summary()
    row = (db_session.query(Notification)
           .filter(Notification.user_id == member.id,
                   Notification.notification_type == NotificationType.DAILY_SUMMARY).one())
    assert row.payload["alerts_still_open"] == 1 and row.payload["alerts_opened"] == 0
