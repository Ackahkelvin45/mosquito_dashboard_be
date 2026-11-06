"""GET /model-performance — scoping, live-only, confidence stats read from the
reported key, missing values excluded (not 0), count agreement vs manual
counts, honest targets, empty state."""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.authentication.enums import UserRole
from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.device.models import MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading
from app.trap_counts.models import ManualTrapCount
from utils.protected_route import get_current_user, get_current_user_or_guest

NOW = datetime.utcnow().replace(microsecond=0)
T0 = NOW - timedelta(days=2)
T1 = T0 + timedelta(days=1)


@pytest.fixture
def app(db_session):
    from app.monitoring.model_performance import router

    application = FastAPI()
    application.include_router(router)
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


def _detection(db, device, ts=T0 + timedelta(hours=1), *, is_test=False, reading=True, **fields):
    event = MosquitoEvent(device_id=device.id, timestamp=ts, count=1, is_test=is_test)
    db.add(event)
    db.flush()
    if reading:
        fields.setdefault("sex", "female")
        db.add(MosquitoIndividualReading(batch_id=event.id, detection_timestamp=ts,
                                         age_group="", **fields))
    db.commit()


@pytest.fixture
def world(make_cluster, make_device, make_user):
    c1, c2 = make_cluster(), make_cluster()
    return {"d1": make_device(c1, name="A trap"), "d2": make_device(c2, name="B trap"),
            "user": make_user(cluster_id=c1.id), "super": make_user(role=UserRole.SUPER_ADMIN)}


def _get(client, days=30):
    r = client.get(f"/model-performance?days={days}")
    assert r.status_code == 200, r.text
    return r.json()


def test_confidence_stats_buckets_missing_and_reported_key(client, as_user, world, db_session):
    d1 = world["d1"]
    _detection(db_session, d1, p_mosq=0.3, genus="aedes", binary_decision=False,
               taxon_probs={"anopheles": 0, "aedes": 0.6, "culex": 0},
               sex_probs={"male": 0, "female": 0.8}, inference_ms=100)
    _detection(db_session, d1, p_mosq=0.5, genus="Aedes", binary_decision=True,
               taxon_probs={"anopheles": 0, "aedes": 1.0, "culex": 0},
               sex="male", sex_probs={"male": 0.7, "female": 0}, inference_ms=300)
    _detection(db_session, d1, p_mosq=0.95, genus="culex", binary_decision=True,
               taxon_probs={"anopheles": 0, "aedes": 0, "culex": 0.9}, inference_ms=-5)
    _detection(db_session, d1, p_mosq=None, genus="culex", taxon_probs=None, sex="")  # all missing
    _detection(db_session, d1, reading=False)                                          # no reading row
    _detection(db_session, d1, p_mosq=0.1, is_test=True)                                # test mode
    _detection(db_session, d1, NOW - timedelta(days=40), p_mosq=0.1)                    # out of window

    as_user(world["user"])
    body = _get(client)
    c = body["confidence"]
    assert c["detections"] == 5
    p = c["p_mosq"]
    assert (p["measured"], p["missing"]) == (3, 2)
    assert p["mean"] == pytest.approx((0.3 + 0.5 + 0.95) / 3, abs=1e-4)  # missing not counted as 0
    assert [b["count"] for b in p["buckets"]] == [1, 1, 0, 1]
    assert (p["below_low"], p["below_low_share"]) == (1, pytest.approx(1 / 3, abs=1e-4))

    g = c["genus_confidence"]
    assert (g["measured"], g["missing"], g["zero_at_reported_key"]) == (3, 2, 0)
    assert g["mean"] == pytest.approx((0.6 + 1.0 + 0.9) / 3, abs=1e-4)
    s = c["sex_confidence"]
    assert (s["measured"], s["missing"], s["mean"]) == (2, 3, pytest.approx(0.75, abs=1e-4))

    assert (c["binary_decision_true"], c["binary_decision_false"], c["binary_decision_missing"]) == (2, 1, 2)
    # -5 ms is invalid → missing, not part of mean/max
    assert c["inference_ms"] == {"measured": 2, "missing": 3, "mean_ms": 200.0, "max_ms": 300}
    assert c["age_group_reported"] == 0
    assert "no detection in this period has an age value" in body["targets"][2]["reason"].lower()
    by_genus = {row["genus"]: row for row in c["by_genus"]}
    assert by_genus["aedes"]["detections"] == 2
    assert by_genus["aedes"]["genus_confidence"]["mean"] == pytest.approx(0.8)
    assert by_genus["culex"]["genus_confidence"]["missing"] == 1
    assert by_genus["unreported"]["detections"] == 1


def test_bucket_boundaries(client, as_user, world, db_session):
    for v in (0.0, 0.4999, 0.5, 0.6999, 0.7, 0.8999, 0.9, 1.0):
        _detection(db_session, world["d1"], p_mosq=v)
    as_user(world["user"])
    p = _get(client)["confidence"]["p_mosq"]
    assert [(b["label"], b["count"]) for b in p["buckets"]] == [
        ("<0.5", 2), ("0.5–0.7", 2), ("0.7–0.9", 2), ("≥0.9", 2)]
    assert p["below_low"] == 2


def test_reads_reported_key_not_the_max(client, as_user, world, db_session):
    _detection(db_session, world["d1"], genus="aedes", taxon_probs={"aedes": 0.4, "culex": 0.9},
               sex="female", sex_probs={"female": 0.3, "male": 0.8})
    _detection(db_session, world["d1"], genus="culex", taxon_probs={"aedes": 0.5, "culex": 0},
               sex="male", sex_probs={"female": 0.6, "male": 0})
    as_user(world["user"])
    c = _get(client)["confidence"]
    assert c["genus_confidence"]["mean"] == pytest.approx(0.2)   # (0.4 + 0) / 2
    assert c["sex_confidence"]["mean"] == pytest.approx(0.15)    # (0.3 + 0) / 2
    assert c["genus_confidence"]["zero_at_reported_key"] == 1
    assert c["sex_confidence"]["zero_at_reported_key"] == 1
    assert c["genus_confidence"]["measured"] == 2


def test_age_reason_follows_data(client, as_user, world, db_session):
    _detection(db_session, world["d1"])
    db_session.query(MosquitoIndividualReading).update({"age_group": "adult"})
    db_session.commit()
    as_user(world["user"])
    body = _get(client)
    assert body["confidence"]["age_group_reported"] == 1
    assert body["targets"][2]["status"] == "not_measurable_yet"
    assert body["targets"][2]["reason"].startswith("1 detections report an age")


def test_scoping(client, as_user, world, db_session):
    _detection(db_session, world["d1"], p_mosq=0.9)
    _detection(db_session, world["d2"], p_mosq=0.9)
    as_user(world["user"])
    assert _get(client)["confidence"]["detections"] == 1
    as_user(world["super"])
    assert _get(client)["confidence"]["detections"] == 2


def test_guest_forbidden_and_days_validated(client, as_user, world, make_user):
    as_user(world["user"])
    assert client.get("/model-performance?days=366").status_code == 422
    as_user(make_user(role=UserRole.GUEST))
    assert client.get("/model-performance").status_code == 403


def test_agreement_with_manual_counts(client, as_user, world, db_session):
    d1, d2 = world["d1"], world["d2"]
    db_session.add(SensorDeviceReading(device_id=d1.id, timestamp=T0 + timedelta(hours=2)))
    for genus, sex, n in [("aedes", "female", 4), ("culex", "male", 2)]:
        db_session.add(ManualTrapCount(device_id=d1.id, period_start=T0, period_end=T1,
                                       genus=genus, sex=sex, count=n, entered_by_user_id=1))
    # Out-of-scope collection, and a collection whose device sent nothing.
    db_session.add(ManualTrapCount(device_id=d2.id, period_start=T0, period_end=T1,
                                   genus="aedes", sex="female", count=9, entered_by_user_id=1))
    db_session.add(ManualTrapCount(device_id=d1.id, period_start=T1, period_end=NOW,
                                   genus="aedes", sex="female", count=7, entered_by_user_id=1))
    db_session.commit()
    for _ in range(2):
        _detection(db_session, d1, genus="aedes", sex="female")
    _detection(db_session, d1, genus="culex", sex="male")
    _detection(db_session, d1, genus="anopheles", sex="female")  # group not counted by hand
    _detection(db_session, d1, genus="aedes", sex="female", is_test=True)

    # Straddles the window start → excluded (must lie fully inside).
    db_session.add(ManualTrapCount(device_id=d1.id, period_start=NOW - timedelta(days=40),
                                   period_end=T0, genus="culex", sex="female", count=3,
                                   entered_by_user_id=1))
    db_session.commit()

    as_user(world["user"])
    a = _get(client)["agreement"]
    assert "whole period" in a["collection_rule"]
    assert len(a["collections"]) == 2
    assert (a["collections_compared"], a["collections_not_reporting"]) == (1, 1)
    assert (a["manual_total"], a["ai_total"], a["abs_difference"]) == (6, 3, 3)
    assert (a["groups_compared"], a["groups_exact_match"], a["exact_match_share"]) == (2, 0, 0)
    assert "count_agreement" not in a
    assert {g["genus"]: g for g in a["by_genus"]} == {
        "aedes": {"genus": "aedes", "groups": 1, "manual": 4, "ai": 2, "abs_difference": 2},
        "culex": {"genus": "culex", "groups": 1, "manual": 2, "ai": 1, "abs_difference": 1},
    }


def test_cancelling_errors_are_not_agreement(client, as_user, world, db_session, make_device):
    """A: manual 10 / AI 0, B: manual 0 / AI 10. Totals match; groups don't."""
    d1, d3 = world["d1"], make_device(world["d1"].cluster, name="C trap")
    for d in (d1, d3):
        db_session.add(SensorDeviceReading(device_id=d.id, timestamp=T0 + timedelta(hours=2)))
    db_session.add(ManualTrapCount(device_id=d1.id, period_start=T0, period_end=T1,
                                   genus="aedes", sex="female", count=10, entered_by_user_id=1))
    db_session.add(ManualTrapCount(device_id=d3.id, period_start=T0, period_end=T1,
                                   genus="aedes", sex="female", count=0, entered_by_user_id=1))
    db_session.commit()
    for _ in range(10):
        _detection(db_session, d3, genus="aedes", sex="female")

    as_user(world["user"])
    a = _get(client)["agreement"]
    assert (a["manual_total"], a["ai_total"]) == (10, 10)
    assert a["abs_difference"] == 20
    assert (a["groups_exact_match"], a["exact_match_share"]) == (0, 0)
    assert a["by_genus"][0]["abs_difference"] == 20


def test_empty_state_and_targets(client, as_user, world):
    as_user(world["user"])
    body = _get(client, days=90)
    assert body["low_confidence_threshold"] == 0.5
    assert body["confidence"]["detections"] == 0
    assert body["confidence"]["p_mosq"]["mean"] is None
    a = body["agreement"]
    assert a["collections"] == [] and a["exact_match_share"] is None and a["by_genus"] == []
    assert [(t["name"], t["target_pct"], t["status"]) for t in body["targets"]] == [
        ("detection", 80, "not_measurable_yet"), ("sex", 75, "not_measurable_yet"),
        ("age", 75, "not_measurable_yet")]
