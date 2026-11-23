"""End-to-end alert lifecycle: a real MQTT message goes through on_message ->
handler -> emit -> cluster fan-out -> channel dispatch, then recipients take
the alert opened -> acknowledged -> closed over the HTTP API."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

import app.core.mqtt_client as mqtt_client
from app.authentication.enums import UserRole
from app.notification.enums import NotificationSeverity, NotificationType
from app.notification.models import Notification, NotificationPreference


@pytest.fixture
def world(db_session, monkeypatch, TestingSessionLocal, make_user, make_cluster, make_device):
    monkeypatch.setattr(mqtt_client, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(mqtt_client, "apply_reported_position", lambda *a, **k: None)
    cluster, other_cluster = make_cluster(), make_cluster()
    w = dict(
        cluster=cluster,
        admin=make_user(UserRole.ADMIN, cluster.id, first_name="Ama", last_name="Admin"),
        member=make_user(UserRole.USER, cluster.id, first_name="Kofi", last_name="Member"),
        root=make_user(UserRole.SUPER_ADMIN, first_name="Sam", last_name="Super"),
        outsider=make_user(UserRole.ADMIN, other_cluster.id),
        device=make_device(cluster),
    )
    # Only the cluster admin opted in to email.
    db_session.add(NotificationPreference(user_id=w["admin"].id, email_enabled=True))
    db_session.commit()
    return w


def _publish(device, kind, payload):
    asyncio.run(mqtt_client.on_message(
        None, f"mosquito_dashboard/{device.device_uuid}/{kind}",
        json.dumps(payload).encode(), 0, None))


def _alert_rows(db_session, notification_type):
    return {n.user_id: n for n in db_session.query(Notification)
            .filter(Notification.notification_type == notification_type)}


def _item(client, row_id, **params):
    items = client.get("/notifications", params=params).json()["items"]
    return next(i for i in items if i["id"] == row_id)


def _seconds(item, field):
    return int((datetime.fromisoformat(item[field])
                - datetime.fromisoformat(item["created_at"])).total_seconds())


def test_species_detection_to_closed(client, login_as, db_session, dispatch_recorder, world):
    admin, member, root = world["admin"], world["member"], world["root"]
    _publish(world["device"], "mosquito_data", {
        "timestamp": "2026-07-30T10:00:00", "sensor_id": world["device"].device_uuid,
        "mosquito_data": [{"detection_timestamp": "2026-07-30T10:00:00",
                           "species": "Anopheles sp.", "genus": "anopheles",
                           "age_group": "adult", "sex": "female"}],
    })

    # Fan-out: cluster admin + member + super admin, one shared alert_group.
    rows = _alert_rows(db_session, NotificationType.SPECIES_DETECTED)
    assert set(rows) == {admin.id, member.id, root.id}
    assert len({r.alert_group for r in rows.values()}) == 1
    assert all(r.severity == NotificationSeverity.CRITICAL for r in rows.values())
    assert all(r.cluster_id == world["cluster"].id for r in rows.values())

    # Channels: push for everyone (default on), email only for the opted-in admin.
    dispatched = {user_id: (push, email) for nid, user_id, push, email in dispatch_recorder
                  if nid in {r.id for r in rows.values()}}
    assert dispatched == {admin.id: (True, True), member.id: (True, False),
                          root.id: (True, False)}

    # Pin created_at so the response times are deterministic.
    db_session.query(Notification).filter(
        Notification.alert_group == rows[admin.id].alert_group
    ).update({"created_at": datetime.utcnow() - timedelta(minutes=10)})
    db_session.commit()

    login_as(member)
    item = _item(client, rows[member.id].id, status="opened")
    assert item["handleable"] and item["time_to_acknowledge_s"] is None
    r = client.patch(f"/notifications/{rows[member.id].id}/acknowledged")
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"

    login_as(root)
    item = _item(client, rows[root.id].id)
    assert item["status"] == "acknowledged"
    assert item["acknowledged_by_name"] == "Kofi Member"
    assert 590 <= item["time_to_acknowledge_s"] <= 610
    assert item["time_to_acknowledge_s"] == _seconds(item, "acknowledged_at")
    assert client.get("/notifications", params={"status": "opened"}).json()["items"] == []

    login_as(admin)
    assert client.patch(f"/notifications/{rows[admin.id].id}/handled").json()["status"] == "closed"

    for user in (member, root, admin):
        login_as(user)
        item = _item(client, rows[user.id].id, status="closed")
        assert item["handled_by_name"] == "Ama Admin"
        assert item["acknowledged_by_name"] == "Kofi Member"
        assert item["time_to_close_s"] == _seconds(item, "handled_at")
        assert item["time_to_acknowledge_s"] == _seconds(item, "acknowledged_at")
        assert item["time_to_close_s"] >= item["time_to_acknowledge_s"] >= 590

    # The outsider's cluster never saw it.
    login_as(world["outsider"])
    assert client.get("/notifications").json()["items"] == []


def test_low_battery_reading_to_closed(client, login_as, db_session, dispatch_recorder, world):
    admin, member = world["admin"], world["member"]
    _publish(world["device"], "sensor_data", {
        "timestamp": "2026-07-30T10:00:00", "sensor_id": world["device"].device_uuid,
        "temp_internal": 26.5, "temp_external": 30.2, "humidity_internal": 60,
        "humidity_external": 75, "pressure_internal": 1010, "external_light": 200.0,
        "battery": 3.1, "trap_status": False,
    })
    rows = _alert_rows(db_session, NotificationType.LOW_BATTERY)
    assert set(rows) == {admin.id, member.id, world["root"].id}
    # WARNING low battery is not an email type, even for the opted-in admin.
    assert all(push and not email for _, _, push, email in dispatch_recorder)

    login_as(admin)
    assert client.patch(f"/notifications/{rows[admin.id].id}/acknowledged").json()["status"] == "acknowledged"
    login_as(member)
    item = _item(client, rows[member.id].id)
    assert item["status"] == "acknowledged" and item["acknowledged_by_name"] == "Ama Admin"
    item = client.patch(f"/notifications/{rows[member.id].id}/handled").json()
    assert item["status"] == "closed" and item["handled_by_name"] == "Kofi Member"
    assert item["time_to_close_s"] >= item["time_to_acknowledge_s"] >= 0
