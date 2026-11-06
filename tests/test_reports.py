"""GET /reports/surveillance — section math from seeded rows, previous-period
comparison (incl. zero previous), test-mode excluded, scope, guest rejection,
alert de-duplication, empty period."""
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.authentication.enums import UserRole
from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.device.models import MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading
from app.notification.enums import NotificationType
from app.reports.routes import period_bounds
from app.trap_counts.models import ManualTrapCount
from utils.protected_route import get_current_user, get_current_user_or_guest, guest_user

END = date(2026, 3, 11)                 # weekly period = [Mar 5, Mar 12)
IN = datetime(2026, 3, 8, 10)           # inside the period
PREV = datetime(2026, 3, 1, 10)         # inside the previous week
BEFORE = datetime(2026, 1, 1)           # devices registered before every test period


@pytest.fixture
def app(db_session):
    from app.reports.routes import router

    application = FastAPI()
    application.include_router(router, prefix="/reports")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def as_user(app):
    def _as(user):
        r = UserResponse.model_validate(user)
        app.dependency_overrides[get_current_user] = lambda: r
        app.dependency_overrides[get_current_user_or_guest] = lambda: r
    return _as


@pytest.fixture
def world(make_cluster, make_device, make_user):
    c1, c2 = make_cluster(), make_cluster()
    return {
        "c1": c1, "c2": c2,
        "d1": make_device(c1, name="A trap", region="Ashanti", community="Kumasi", created_at=BEFORE),
        "d2": make_device(c1, name="B trap", region="Volta", community="Ho", created_at=BEFORE),
        "other": make_device(c2, name="Other trap", region="Ashanti", created_at=BEFORE),
        "user": make_user(cluster_id=c1.id),
        "super": make_user(role=UserRole.SUPER_ADMIN),
    }


def _event(db, device, ts, *, count=1, is_test=False, genus="aedes", sex="female", species=None):
    e = MosquitoEvent(device_id=device.id, timestamp=ts, count=count, is_test=is_test)
    db.add(e)
    db.flush()
    db.add(MosquitoIndividualReading(batch_id=e.id, detection_timestamp=ts, genus=genus, sex=sex,
                                     species=species, age_group=""))
    db.commit()


def _reading(db, device, ts, *, is_test=False, **fields):
    db.add(SensorDeviceReading(device_id=device.id, timestamp=ts, is_test=is_test, **fields))
    db.commit()


def _get(client, **params):
    params.setdefault("end", END.isoformat())
    r = client.get("/reports/surveillance", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_period_bounds():
    assert period_bounds("weekly", END) == (datetime(2026, 3, 5), datetime(2026, 3, 12))
    assert period_bounds("monthly", END) == (datetime(2026, 3, 1), datetime(2026, 4, 1))
    assert period_bounds("monthly", date(2025, 12, 31)) == (datetime(2025, 12, 1), datetime(2026, 1, 1))


def test_section_math_and_test_mode_excluded(client, as_user, world, db_session):
    d1, d2, other = world["d1"], world["d2"], world["other"]
    _event(db_session, d1, IN, count=2, species="Aedes aegypti")
    _event(db_session, d1, IN + timedelta(days=1), genus="anopheles", sex="male")
    _event(db_session, d2, IN, genus="culex")
    _event(db_session, d1, IN, count=50, is_test=True)              # test mode: ignored
    _event(db_session, other, IN, count=30)                          # other cluster: ignored
    _event(db_session, d1, datetime(2026, 3, 12), count=9)           # exactly at period end: next period
    _event(db_session, d1, PREV)                                     # previous period: 1
    _reading(db_session, d1, IN, external_temperature=20.0, internal_temperature=99.0, external_humidity=60.0)
    _reading(db_session, d1, IN + timedelta(hours=1), internal_temperature=30.0, internal_humidity=80.0)
    _reading(db_session, d1, IN, is_test=True, external_temperature=-40.0)  # test mode: ignored
    _reading(db_session, other, IN, external_temperature=50.0)              # other cluster

    as_user(world["user"])
    body = _get(client)
    s = body["summary"]
    assert (s["detections"], s["detection_events"], s["previous_detections"]) == (4, 3, 1)
    assert (s["no_device_reported"], s["previous_devices_reporting"]) == (False, 1)
    assert (s["change"], s["change_pct"]) == (3, 300.0)
    assert s["top_species"] == "Aedes aegypti"
    assert (s["devices_reporting"], s["devices_total"]) == (2, 2)
    assert s["regions_covered"] == ["Ashanti", "Volta"]
    assert body["period_complete"] is True and body["period_end"] == "2026-03-12T00:00:00"

    det = body["detections"]
    assert {g["name"]: g["count"] for g in det["by_genus"]} == {"aedes": 2, "anopheles": 1, "culex": 1}
    assert {g["name"]: g["count"] for g in det["by_sex"]} == {"female": 3, "male": 1}
    assert {g["name"]: g["count"] for g in det["by_species"]} == {"Aedes aegypti": 2, "Unknown": 2}
    assert {r["region"]: r["count"] for r in det["by_region"]} == {"Ashanti": 3, "Volta": 1}
    assert len(det["daily"]) == 7
    by_day = {p["date"]: (p["count"], p["devices_reporting"]) for p in det["daily"]}
    assert by_day["2026-03-08"] == (3, 2) and by_day["2026-03-09"] == (1, 1)
    assert by_day["2026-03-05"] == (None, 0)  # nobody reported: no value, not 0
    assert det["unclassified"] == 0

    env = body["environment"]
    assert env["readings"] == 2
    t, h = env["temperature"], env["humidity"]
    assert (t["readings"], t["mean"], t["min"], t["max"]) == (2, 25.0, 20.0, 30.0)  # external, else internal
    assert (h["readings"], h["mean"], h["min"], h["max"]) == (2, 70.0, 60.0, 80.0)

    dq = body["data_quality"]
    assert dq["devices_checked"] == 2
    assert [d["name"] for d in dq["no_readings"]] == ["B trap"]


def test_zero_previous_gives_null_pct(client, as_user, world, db_session):
    _event(db_session, world["d1"], IN)
    as_user(world["user"])
    s = _get(client)["summary"]
    assert (s["detections"], s["previous_detections"], s["change"], s["change_pct"]) == (1, 0, 1, None)


def test_monthly_compares_previous_calendar_month(client, as_user, world, db_session):
    _event(db_session, world["d1"], datetime(2026, 3, 31, 23), count=3)
    _event(db_session, world["d1"], datetime(2026, 2, 1), count=6)
    _event(db_session, world["d1"], datetime(2026, 4, 1), count=100)
    as_user(world["user"])
    body = _get(client, period="monthly")
    assert body["previous_start"] == "2026-02-01T00:00:00" and body["previous_end"] == "2026-03-01T00:00:00"
    s = body["summary"]
    assert (s["detections"], s["previous_detections"], s["change_pct"]) == (3, 6, -50.0)
    assert len(body["detections"]["daily"]) == 31


def test_scope_and_filters(client, as_user, world, db_session):
    _event(db_session, world["d1"], IN)
    _event(db_session, world["other"], IN, count=5)
    as_user(world["user"])
    assert client.get("/reports/surveillance", params={"cluster_id": world["c2"].id}).status_code == 404
    assert _get(client, region="volta")["summary"]["devices_total"] == 1  # case-insensitive exact
    assert _get(client, region="volta")["summary"]["detections"] == 0

    as_user(world["super"])
    assert _get(client)["summary"]["detections"] == 6
    assert _get(client)["scope"] == "All clusters"
    body = _get(client, cluster_id=world["c2"].id, region="Ashanti")
    assert body["summary"]["detections"] == 5
    assert body["scope"].endswith("· Region: Ashanti")


def test_guest_and_anonymous_rejected(client, app):
    assert client.get("/reports/surveillance").status_code == 401
    app.dependency_overrides[get_current_user] = guest_user
    assert client.get("/reports/surveillance").status_code == 403


def test_future_end_rejected(client, as_user, world):
    as_user(world["user"])
    future = (datetime.utcnow() + timedelta(days=2)).date().isoformat()
    assert client.get("/reports/surveillance", params={"end": future}).status_code == 422


def test_alerts_deduplicated_per_group(client, as_user, world, make_notification, make_user):
    u, u2 = world["user"], make_user(cluster_id=world["c1"].id)
    d1 = world["d1"]
    kw = dict(device_id=d1.id, created_at=IN)
    # One emission to two recipients, handled once → 1 alert, handled.
    make_notification(u, notification_type=NotificationType.LOW_BATTERY, alert_group="g1", handled_at=IN, **kw)
    make_notification(u2, notification_type=NotificationType.LOW_BATTERY, alert_group="g1", handled_at=IN, **kw)
    make_notification(u, notification_type=NotificationType.LOW_BATTERY, alert_group="g2", **kw)
    make_notification(u, notification_type=NotificationType.DEVICE_OFFLINE, alert_group=None, **kw)  # legacy
    make_notification(u, notification_type=NotificationType.DAILY_SUMMARY, alert_group="g3", **kw)   # not an alert
    make_notification(u, notification_type=NotificationType.LOW_BATTERY, alert_group="g4",
                      device_id=world["other"].id, created_at=IN)                                      # out of scope
    make_notification(u, notification_type=NotificationType.LOW_BATTERY, alert_group="g5",
                      device_id=d1.id, created_at=PREV)                                                # other period
    as_user(u)
    a = _get(client)["alerts"]
    assert (a["total"], a["handled"], a["ungrouped_rows"]) == (3, 1, 1)
    assert a["by_type"] == [{"type": "LOW_BATTERY", "count": 2, "handled": 1},
                            {"type": "DEVICE_OFFLINE", "count": 1, "handled": 0}]


def test_manual_vs_ai(client, as_user, world, db_session):
    d1 = world["d1"]
    _reading(db_session, d1, IN)
    _event(db_session, d1, IN, genus="aedes", sex="female")
    _event(db_session, d1, IN, genus="aedes", sex="female")
    db_session.add(ManualTrapCount(device_id=d1.id, period_start=datetime(2026, 3, 6),
                                   period_end=datetime(2026, 3, 10), genus="aedes", sex="female",
                                   count=3, entered_by_user_id=world["user"].id))
    db_session.commit()
    as_user(world["user"])
    m = _get(client)["manual_vs_ai"]
    assert (m["collections"], m["manual_total"], m["ai_total"], m["abs_difference"]) == (1, 3, 2, 1)
    assert m["message"] is None


def test_empty_period(client, as_user, world):
    as_user(world["user"])
    body = _get(client)
    s = body["summary"]
    assert (s["detections"], s["change_pct"], s["devices_reporting"], s["regions_covered"]) == (0, None, 0, [])
    assert s["no_device_reported"] is True
    assert all(p["count"] is None for p in body["detections"]["daily"])
    assert body["detections"]["by_genus"] == [] and body["detections"]["by_species"] == []
    assert body["environment"]["temperature"] == {"readings": 0, "mean": None, "min": None, "max": None}
    assert body["alerts"]["total"] == 0 and body["alerts"]["by_type"] == []
    assert body["manual_vs_ai"]["message"] == "No manual trap counts this period"
    assert len(body["data_quality"]["no_readings"]) == 2


def test_incomplete_period_has_no_comparison(client, as_user, world, db_session):
    today = datetime.utcnow().date()
    _event(db_session, world["d1"], datetime.combine(today, datetime.min.time()))
    _event(db_session, world["d1"], datetime.combine(today, datetime.min.time()) - timedelta(days=10))
    as_user(world["user"])
    body = _get(client, end=today.isoformat())
    assert body["period_complete"] is False
    assert (body["summary"]["change"], body["summary"]["change_pct"]) == (None, None)
    assert body["detections"]["daily"][-1]["date"] == today.isoformat()  # no future days


def test_device_created_after_period_excluded(client, as_user, world, make_device):
    make_device(world["c1"], name="New trap", created_at=datetime(2026, 3, 12))
    as_user(world["user"])
    body = _get(client)
    assert body["summary"]["devices_total"] == 2
    assert "New trap" not in [d["name"] for d in body["data_quality"]["no_readings"]]


def test_test_mode_only_device_is_not_reporting(client, as_user, world, db_session):
    _reading(db_session, world["d1"], IN, is_test=True, external_temperature=20.0)
    _event(db_session, world["d1"], IN, is_test=True)
    as_user(world["user"])
    s = _get(client)["summary"]
    assert (s["devices_reporting"], s["no_device_reported"]) == (0, True)


def test_external_humidity_wins_over_internal(client, as_user, world, db_session):
    _reading(db_session, world["d1"], IN, external_humidity=40.0, internal_humidity=90.0)
    as_user(world["user"])
    h = _get(client)["environment"]["humidity"]
    assert (h["readings"], h["mean"]) == (1, 40.0)


def test_alerts_and_data_quality_use_own_cluster_only(client, as_user, make_cluster, make_device,
                                                     make_user, make_notification):
    own, pub = make_cluster(), make_cluster(public=True)
    d_own = make_device(own, name="Own trap", created_at=BEFORE)
    d_pub = make_device(pub, name="Public trap", created_at=BEFORE)
    user = make_user(cluster_id=own.id)
    for d in (d_own, d_pub):
        make_notification(user, notification_type=NotificationType.LOW_BATTERY,
                          alert_group=f"g{d.id}", device_id=d.id, created_at=IN)
    as_user(user)
    body = _get(client)
    assert body["summary"]["devices_total"] == 2              # public cluster is visible data
    assert body["alerts"]["total"] == 1                       # ...but not its alerts
    assert [d["name"] for d in body["data_quality"]["no_readings"]] == ["Own trap"]
    assert body["management_scope"].startswith("Your own cluster")

    as_user(make_user(role=UserRole.SUPER_ADMIN))
    body = _get(client)
    assert body["alerts"]["total"] == 2 and body["management_scope"] == "All clusters"


def test_region_scope_text_when_no_devices(client, as_user, world):
    as_user(world["user"])
    assert "No devices in region 'Nowhere'" in _get(client, region="Nowhere")["scope"]
