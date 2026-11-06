"""/trap-counts — create/validate/permissions, audit rows, and the manual vs
AI comparison (live-only, in-period-only, sex ""→unknown, device-reporting)."""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.audit.models import AuditAction, AuditLog
from app.authentication.enums import UserRole
from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.device.models import MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading
from app.trap_counts.models import ManualTrapCount
from utils.protected_route import get_current_user, get_current_user_or_guest

T0 = datetime.utcnow().replace(microsecond=0) - timedelta(days=2)
T1 = T0 + timedelta(days=1)


@pytest.fixture
def app(db_session):
    from app.trap_counts.routes import router

    application = FastAPI()
    application.include_router(router, prefix="/trap-counts")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def as_user(app):
    def _as(user):
        response_user = UserResponse.model_validate(user)
        app.dependency_overrides[get_current_user] = lambda: response_user
        app.dependency_overrides[get_current_user_or_guest] = lambda: response_user
    return _as


@pytest.fixture
def world(make_cluster, make_device, make_user):
    c1, c2, pub = make_cluster(), make_cluster(), make_cluster(public=True)
    return {
        "d1": make_device(c1), "d2": make_device(c2), "dpub": make_device(pub),
        "user": make_user(cluster_id=c1.id),
        "other": make_user(cluster_id=c2.id),
        "admin": make_user(role=UserRole.ADMIN, cluster_id=c1.id),
        "admin2": make_user(role=UserRole.ADMIN, cluster_id=c2.id),
        "super": make_user(role=UserRole.SUPER_ADMIN),
    }


def _body(device, entries=None, start=T0, end=T1):
    return {"device_id": device.id, "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "entries": entries or [{"genus": "aedes", "sex": "female", "count": 5}]}


def _detection(db, device, ts, *, genus="aedes", sex="female", is_test=False, reading=True):
    event = MosquitoEvent(device_id=device.id, timestamp=ts, count=1, is_test=is_test)
    db.add(event)
    db.flush()
    if reading:
        db.add(MosquitoIndividualReading(batch_id=event.id, detection_timestamp=ts,
                                         genus=genus, sex=sex, age_group=""))
    db.commit()


# ── Create / validate / permissions ──────────────────────────────────────────

def test_user_creates_counts_for_own_cluster_and_audits(client, as_user, world, db_session):
    as_user(world["user"])
    r = client.post("/trap-counts", json=_body(world["d1"], [
        {"genus": "aedes", "sex": "female", "count": 5},
        {"genus": "culex", "sex": "male", "count": 0, "notes": "wet"},
    ]))
    assert r.status_code == 201, r.text
    assert [(x["genus"], x["sex"], x["count"]) for x in r.json()] == [("aedes", "female", 5), ("culex", "male", 0)]
    assert all(x["entered_by_user_id"] == world["user"].id for x in r.json())
    logs = db_session.query(AuditLog).filter(AuditLog.action == AuditAction.TRAP_COUNT_CREATED).all()
    assert len(logs) == 2 and logs[0].actor_user_id == world["user"].id


def test_aware_timestamps_stored_as_naive_utc(client, as_user, world, db_session):
    as_user(world["user"])
    body = _body(world["d1"])
    body["period_start"] = (T0.isoformat() + "+02:00")
    assert client.post("/trap-counts", json=body).status_code == 201
    assert db_session.query(ManualTrapCount).one().period_start == T0 - timedelta(hours=2)


@pytest.mark.parametrize("change, code", [
    ({"period_end": T0.isoformat()}, 422),                                         # end == start
    ({"period_end": (datetime.utcnow() + timedelta(hours=1)).isoformat()}, 422),   # future
    ({"entries": [{"genus": "aedes", "sex": "female", "count": -1}]}, 422),
    ({"entries": [{"genus": "bogus", "sex": "female", "count": 1}]}, 422),
    ({"entries": [{"genus": "aedes", "sex": "x", "count": 1}]}, 422),
    ({"entries": []}, 422),
    ({"entries": [{"genus": "aedes", "sex": "male", "count": 1}] * 2}, 422),       # duplicate group
    ({"device_id": 999999}, 404),
])
def test_create_validation(client, as_user, world, change, code):
    as_user(world["user"])
    assert client.post("/trap-counts", json={**_body(world["d1"]), **change}).status_code == code


def test_duplicate_group_for_same_collection_conflicts(client, as_user, world, db_session):
    as_user(world["user"])
    assert client.post("/trap-counts", json=_body(world["d1"])).status_code == 201
    r = client.post("/trap-counts", json=_body(world["d1"]))  # same device/period/genus/sex
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    assert db_session.query(ManualTrapCount).count() == 1
    # Same period, new group = same collection, allowed.
    assert client.post("/trap-counts", json=_body(world["d1"], [
        {"genus": "culex", "sex": "male", "count": 2}])).status_code == 201


def test_overlapping_period_conflicts_back_to_back_allowed(client, as_user, world):
    as_user(world["user"])
    assert client.post("/trap-counts", json=_body(world["d1"])).status_code == 201
    mid = T0 + timedelta(hours=12)
    r = client.post("/trap-counts", json=_body(world["d1"], start=mid, end=T1 + timedelta(hours=1)))
    assert r.status_code == 409 and "overlaps an existing collection" in r.json()["detail"]
    r = client.post("/trap-counts", json=_body(world["d1"], start=T0 - timedelta(hours=1), end=mid))
    assert r.status_code == 409
    # Back-to-back (half-open periods) and other devices don't overlap.
    assert client.post("/trap-counts", json=_body(world["d1"], start=T1, end=T1 + timedelta(hours=1))).status_code == 201
    assert client.post("/trap-counts", json=_body(world["d1"], start=T0 - timedelta(hours=1), end=T0)).status_code == 201
    assert client.post("/trap-counts", json=_body(world["dpub"], start=mid, end=T1)).status_code == 403


def test_user_cannot_add_outside_own_cluster(client, as_user, world):
    as_user(world["user"])
    assert client.post("/trap-counts", json=_body(world["d2"])).status_code == 404     # private, other
    assert client.post("/trap-counts", json=_body(world["dpub"])).status_code == 403   # public, not theirs


def test_guest_cannot_write_or_read(client):
    # No auth header → get_current_user rejects outright.
    assert client.post("/trap-counts", json={"device_id": 1}).status_code in (401, 403)
    assert client.get("/trap-counts").status_code in (401, 403)


def test_guest_principal_rejected_on_create(client, app, world):
    from utils.protected_route import guest_user
    app.dependency_overrides[get_current_user] = guest_user
    assert client.post("/trap-counts", json=_body(world["dpub"])).status_code == 403


def test_super_admin_can_add_anywhere(client, as_user, world):
    as_user(world["super"])
    assert client.post("/trap-counts", json=_body(world["d2"])).status_code == 201


# ── Edit / delete ─────────────────────────────────────────────────────────────

def _seed(client, as_user, user, device):
    as_user(user)
    r = client.post("/trap-counts", json=_body(device))
    assert r.status_code == 201
    return r.json()[0]["id"]


def test_admin_edits_and_deletes_own_cluster_with_audit(client, as_user, world, db_session):
    cid = _seed(client, as_user, world["user"], world["d1"])
    as_user(world["admin"])
    r = client.patch(f"/trap-counts/{cid}", json={"count": 7, "notes": "recount"})
    assert r.status_code == 200 and r.json()["count"] == 7 and r.json()["notes"] == "recount"
    assert client.delete(f"/trap-counts/{cid}").status_code == 204
    assert db_session.query(ManualTrapCount).count() == 0
    actions = [a.action for a in db_session.query(AuditLog).order_by(AuditLog.id)]
    assert actions == [AuditAction.TRAP_COUNT_CREATED, AuditAction.TRAP_COUNT_UPDATED,
                       AuditAction.TRAP_COUNT_DELETED]
    upd = db_session.query(AuditLog).filter(AuditLog.action == AuditAction.TRAP_COUNT_UPDATED).one()
    assert upd.detail == {"before": {"count": 5, "notes": None}, "after": {"count": 7, "notes": "recount"}}


def test_admin_cannot_edit_other_cluster(client, as_user, world):
    cid = _seed(client, as_user, world["user"], world["d1"])
    as_user(world["admin2"])
    assert client.patch(f"/trap-counts/{cid}", json={"count": 1}).status_code == 404
    assert client.delete(f"/trap-counts/{cid}").status_code == 404


def test_admin_cannot_edit_public_cluster_not_theirs(client, as_user, world):
    cid = _seed(client, as_user, world["super"], world["dpub"])
    as_user(world["admin"])
    assert client.patch(f"/trap-counts/{cid}", json={"count": 1}).status_code == 403


def test_plain_user_cannot_edit_or_delete(client, as_user, world):
    cid = _seed(client, as_user, world["user"], world["d1"])
    assert client.patch(f"/trap-counts/{cid}", json={"count": 1}).status_code == 403
    assert client.delete(f"/trap-counts/{cid}").status_code == 403


def test_super_admin_edits_anywhere(client, as_user, world):
    cid = _seed(client, as_user, world["other"], world["d2"])
    as_user(world["super"])
    assert client.patch(f"/trap-counts/{cid}", json={"count": 2}).status_code == 200
    assert client.patch(f"/trap-counts/{cid}", json={"count": -2}).status_code == 422
    assert client.patch(f"/trap-counts/{cid}", json={"count": None}).status_code == 422
    assert client.patch("/trap-counts/999999", json={"count": 2}).status_code == 404


# ── Reads + comparison ───────────────────────────────────────────────────────

def test_list_is_scoped(client, as_user, world):
    _seed(client, as_user, world["user"], world["d1"])
    _seed(client, as_user, world["other"], world["d2"])
    as_user(world["user"])
    assert [c["device_id"] for c in client.get("/trap-counts").json()] == [world["d1"].id]
    as_user(world["super"])
    assert len(client.get("/trap-counts").json()) == 2
    assert len(client.get(f"/trap-counts?device_id={world['d2'].id}").json()) == 1


def test_empty_list(client, as_user, world):
    as_user(world["user"])
    assert client.get("/trap-counts").json() == []


def test_comparison_math(client, as_user, world, db_session):
    d = world["d1"]
    mid = T0 + timedelta(hours=6)
    _detection(db_session, d, mid)                                   # aedes/female
    _detection(db_session, d, mid, genus="Aedes")                    # case-normalised → aedes/female
    _detection(db_session, d, mid, sex="")                           # aedes/unknown
    _detection(db_session, d, T0, genus="culex", sex="male")         # start boundary, included
    _detection(db_session, d, T1 - timedelta(seconds=1), genus="unknownus", sex="male")  # → other/male
    _detection(db_session, d, T1)                                    # end boundary, excluded (half-open)
    _detection(db_session, d, mid, reading=False)                    # no reading row → other/unknown
    _detection(db_session, d, mid, is_test=True)                     # test mode, excluded
    _detection(db_session, d, T0 - timedelta(seconds=1))             # before period, excluded
    _detection(db_session, d, T1 + timedelta(seconds=1))             # after period, excluded
    _detection(db_session, world["d2"], mid)                         # other device, excluded
    db_session.add(SensorDeviceReading(device_id=d.id, timestamp=mid))
    db_session.commit()

    as_user(world["user"])
    assert client.post("/trap-counts", json=_body(d, [
        {"genus": "aedes", "sex": "female", "count": 3},
        {"genus": "anopheles", "sex": "female", "count": 1},
    ])).status_code == 201

    [col] = client.get("/trap-counts").json()
    groups = {(g["genus"], g["sex"]): (g["manual"], g["ai"], g["difference"]) for g in col["groups"]}
    # Only entered groups are compared; nothing is invented as a manual 0.
    assert groups == {
        ("anopheles", "female"): (1, 0, -1),
        ("aedes", "female"): (3, 2, -1),
    }
    assert (col["manual_total"], col["ai_total"], col["difference"]) == (4, 2, -2)
    assert col["ai_not_counted"] == [
        {"genus": "aedes", "sex": "unknown", "ai": 1},
        {"genus": "culex", "sex": "male", "ai": 1},
        {"genus": "other", "sex": "male", "ai": 1},
        {"genus": "other", "sex": "unknown", "ai": 1},
    ]
    assert col["device_reporting"] is True
    assert len(col["entries"]) == 2


def test_device_not_reporting_flag(client, as_user, world, db_session):
    d = world["d1"]
    # Only out-of-period and test-mode sensor readings → not reporting.
    db_session.add(SensorDeviceReading(device_id=d.id, timestamp=T0 - timedelta(hours=1)))
    db_session.add(SensorDeviceReading(device_id=d.id, timestamp=T0 + timedelta(hours=1), is_test=True))
    db_session.add(SensorDeviceReading(device_id=d.id, timestamp=T1))  # end excluded (half-open)
    db_session.commit()
    _seed(client, as_user, world["user"], d)
    [col] = client.get("/trap-counts").json()
    assert col["device_reporting"] is False
    assert (col["manual_total"], col["ai_total"], col["difference"]) == (5, 0, -5)
    assert col["ai_not_counted"] == []
