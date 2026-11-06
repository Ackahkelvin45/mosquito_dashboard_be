"""Mark-handled edge cases: group stamping, legacy-row isolation, filter pagination, archived siblings."""
from app.notification.enums import NotificationCategory, NotificationSeverity, NotificationType
from app.notification.models import Notification

KW = dict(title="t", body="b", notification_type=NotificationType.LOW_BATTERY,
          severity=NotificationSeverity.WARNING, category=NotificationCategory.SENSOR)


def test_send_bulk_shares_one_group_and_send_gets_distinct(service, db_session, make_user):
    a, b, c = make_user(), make_user(), make_user()
    service.send_bulk([a.id, b.id, c.id], **KW)
    groups = {n.alert_group for n in db_session.query(Notification).all()}
    assert len(groups) == 1 and None not in groups
    service.send(a.id, **KW)
    service.send(a.id, **KW)
    assert len({n.alert_group for n in db_session.query(Notification).all()}) == 3


def test_unhandled_on_null_group_row_leaves_other_null_rows(client, login_as, make_user,
                                                            make_notification, db_session):
    # A handled legacy (NULL-group) row for bob must survive alice un-handling hers.
    alice, bob = make_user(), make_user()
    a = make_notification(alice, notification_type=NotificationType.DEVICE_OFFLINE)
    b = make_notification(bob, notification_type=NotificationType.DEVICE_OFFLINE)
    login_as(bob)
    assert client.patch(f"/notifications/{b.id}/handled").status_code == 200
    login_as(alice)
    assert client.patch(f"/notifications/{a.id}/handled").status_code == 200
    assert client.patch(f"/notifications/{a.id}/unhandled").status_code == 200
    db_session.expire_all()
    assert db_session.get(Notification, b.id).handled_by_user_id == bob.id


def test_unhandled_filter_paginates_and_is_user_scoped(client, login_as, service, db_session,
                                                      make_user):
    me, other = make_user(), make_user()
    for _ in range(5):
        service.send_bulk([me.id, other.id], **KW)
    service.send(other.id, **KW)  # other-only row must never leak into my list
    mine = (db_session.query(Notification).filter(Notification.user_id == me.id)
            .order_by(Notification.id).all())
    login_as(me)
    client.patch(f"/notifications/{mine[0].id}/handled")

    p1 = client.get("/notifications?handled=false&page=1&page_size=2").json()
    p2 = client.get("/notifications?handled=false&page=2&page_size=2").json()
    assert p1["total"] == 4 and p1["total_pages"] == 2
    ids = [i["id"] for i in p1["items"] + p2["items"]]
    assert sorted(ids) == sorted(n.id for n in mine[1:])
    handled = client.get("/notifications?handled=true").json()
    assert [i["id"] for i in handled["items"]] == [mine[0].id]  # not other's sibling row


def test_archived_sibling_still_stamped(client, login_as, service, db_session, make_user):
    a, b = make_user(), make_user()
    service.send_bulk([a.id, b.id], **KW)
    ra = db_session.query(Notification).filter(Notification.user_id == a.id).one()
    rb = db_session.query(Notification).filter(Notification.user_id == b.id).one()
    login_as(b)
    client.patch(f"/notifications/{rb.id}/archive")
    login_as(a)
    client.patch(f"/notifications/{ra.id}/handled")
    login_as(b)
    archived = client.get("/notifications?archived=true&handled=true").json()
    assert [i["id"] for i in archived["items"]] == [rb.id]
