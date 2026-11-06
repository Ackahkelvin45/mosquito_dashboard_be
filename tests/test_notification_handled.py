"""Marking alerts handled: shared across every recipient of one emission."""
from app.audit.models import AuditAction, AuditLog
from app.notification.enums import (
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
)
from app.notification.models import Notification


def _alert(service, cluster_id, notification_type=NotificationType.LOW_BATTERY):
    service.notify_cluster(
        cluster_id,
        title="Low battery", body="Battery low",
        notification_type=notification_type,
        severity=NotificationSeverity.WARNING,
        category=NotificationCategory.SENSOR,
    )


def _row(db_session, user):
    return db_session.query(Notification).filter(Notification.user_id == user.id).one()


def test_handle_is_shared_with_sibling_recipients(client, login_as, service, db_session,
                                                  make_user, make_cluster):
    cluster = make_cluster()
    alice = make_user(cluster_id=cluster.id, first_name="Alice", last_name="Ade")
    bob = make_user(cluster_id=cluster.id)
    _alert(service, cluster.id)
    _alert(service, cluster.id)  # a separate emission — must stay unhandled
    alice_row, other_alert = (db_session.query(Notification)
                              .filter(Notification.user_id == alice.id)
                              .order_by(Notification.id).all())

    login_as(alice)
    response = client.patch(f"/notifications/{alice_row.id}/handled")
    assert response.status_code == 200
    assert response.json()["handled_by_name"] == "Alice Ade"
    assert response.json()["handled_at"] is not None

    login_as(bob)
    items = {i["id"]: i for i in client.get("/notifications").json()["items"]}
    bob_rows = sorted(items)
    assert items[bob_rows[0]]["handled_by_name"] == "Alice Ade"
    assert items[bob_rows[1]]["handled_at"] is None
    assert items[bob_rows[0]]["handleable"] is True

    # Second click by someone else does not overwrite the original handler.
    client.patch(f"/notifications/{bob_rows[0]}/handled")
    db_session.expire_all()
    assert db_session.get(Notification, alice_row.id).handled_by_user_id == alice.id
    assert db_session.get(Notification, other_alert.id).handled_at is None

    # Undo clears it for everyone.
    assert client.patch(f"/notifications/{bob_rows[0]}/unhandled").status_code == 200
    db_session.expire_all()
    assert db_session.get(Notification, alice_row.id).handled_at is None

    actions = [r.action for r in db_session.query(AuditLog).all()]
    assert actions.count(AuditAction.ALERT_HANDLED) == 1  # bob's no-op click isn't audited
    assert AuditAction.ALERT_UNHANDLED in actions


def test_non_recipient_gets_404(client, login_as, service, db_session, make_user, make_cluster):
    cluster, other = make_cluster(), make_cluster()
    member = make_user(cluster_id=cluster.id)
    outsider = make_user(cluster_id=other.id)
    _alert(service, cluster.id)
    row = _row(db_session, member)

    login_as(outsider)
    assert client.patch(f"/notifications/{row.id}/handled").status_code == 404
    db_session.expire_all()
    assert db_session.get(Notification, row.id).handled_at is None


def test_non_alert_cannot_be_handled(client, login_as, make_user, make_notification):
    user = make_user()
    row = make_notification(user)  # TEST type — informational
    login_as(user)
    assert client.patch(f"/notifications/{row.id}/handled").status_code == 400


def test_legacy_row_without_group_handles_only_itself(client, login_as, make_user,
                                                      make_notification, db_session):
    alice, bob = make_user(), make_user()
    a = make_notification(alice, notification_type=NotificationType.DEVICE_OFFLINE)
    b = make_notification(bob, notification_type=NotificationType.DEVICE_OFFLINE)
    login_as(alice)
    assert client.patch(f"/notifications/{a.id}/handled").status_code == 200
    db_session.expire_all()
    assert db_session.get(Notification, b.id).handled_at is None


def test_handled_filter(client, login_as, service, db_session, make_user, make_cluster,
                        make_notification):
    cluster = make_cluster()
    user = make_user(cluster_id=cluster.id)
    _alert(service, cluster.id)
    _alert(service, cluster.id)
    make_notification(user)  # informational: in neither handled bucket
    first = (db_session.query(Notification)
             .filter(Notification.user_id == user.id,
                     Notification.notification_type == NotificationType.LOW_BATTERY)
             .order_by(Notification.id).first())
    login_as(user)
    client.patch(f"/notifications/{first.id}/handled")

    handled = client.get("/notifications?handled=true").json()
    unhandled = client.get("/notifications?handled=false").json()
    assert [i["id"] for i in handled["items"]] == [first.id]
    assert unhandled["total"] == 1 and unhandled["items"][0]["id"] != first.id
    assert client.get("/notifications").json()["total"] == 3
