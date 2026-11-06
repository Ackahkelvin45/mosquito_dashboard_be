"""GET /monitoring/data-quality — scoping, every metric from seeded rows,
test-mode rows excluded, silence (gap) counting."""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.authentication.enums import UserRole
from app.core.database import get_db
from app.device.models import MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading
from utils.protected_route import get_current_user, get_current_user_or_guest


@pytest.fixture
def app(db_session):
    from app.monitoring.data_quality import router

    application = FastAPI()
    application.include_router(router, prefix="/monitoring")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _as(app, user):
    from app.authentication.schema import UserResponse
    response_user = UserResponse.model_validate(user)
    app.dependency_overrides[get_current_user] = lambda: response_user
    app.dependency_overrides[get_current_user_or_guest] = lambda: response_user


def _reading(db, device, ts, *, is_test=False, **fields):
    db.add(SensorDeviceReading(device_id=device.id, timestamp=ts, is_test=is_test, **fields))


def _detection(db, device, ts, *, is_test=False, reading=True, **fields):
    event = MosquitoEvent(device_id=device.id, timestamp=ts, count=1, is_test=is_test)
    db.add(event)
    db.flush()
    if reading:
        db.add(MosquitoIndividualReading(batch_id=event.id, detection_timestamp=ts,
                                         age_group="", sex="female", **fields))


@pytest.fixture
def world(db_session, make_cluster, make_device, make_user):
    c1, c2 = make_cluster(), make_cluster()
    d1 = make_device(c1, name="A trap")   # the fully-seeded device
    d3 = make_device(c1, name="B trap")   # a single reading
    d2 = make_device(c2, name="C trap")   # nothing at all

    t0 = datetime.utcnow() - timedelta(hours=3)
    m = lambda minutes: t0 + timedelta(minutes=minutes)  # noqa: E731
    # Live readings at 0, 5, 25, 30, 60 min → gaps 5, 20, 5, 30.
    _reading(db_session, d1, m(0), external_temperature=25.0, external_humidity=99.0, battery_pct=101)
    _reading(db_session, d1, m(5), external_temperature=50.0, external_humidity=70.0, battery_pct=-1)
    # External wins over internal (100 °C internal is NOT counted).
    _reading(db_session, d1, m(25), external_temperature=25.0, internal_temperature=100.0,
             external_humidity=70.0, battery_pct=50)
    _reading(db_session, d1, m(30), external_temperature=25.0, external_humidity=70.0, battery_pct=50)
    # No external → falls back to internal 2 °C, which is out of range.
    _reading(db_session, d1, m(60), internal_temperature=2.0, external_humidity=70.0, battery_pct=50)
    # Test reading inside the 30-min gap: would split it into 15+15 and add
    # an out-of-range temperature if it leaked in.
    _reading(db_session, d1, m(45), is_test=True, external_temperature=99.0, external_humidity=70.0)
    # Outside the 7-day window.
    _reading(db_session, d1, datetime.utcnow() - timedelta(days=10), external_temperature=99.0)

    _reading(db_session, d3, m(0), external_temperature=20.0)

    _detection(db_session, d1, m(1), p_mosq=0.9, genus="aedes")
    _detection(db_session, d1, m(2), p_mosq=None, genus=None)
    _detection(db_session, d1, m(3), p_mosq=1.5, genus="  ")
    _detection(db_session, d1, m(4), reading=False)  # event with no reading row
    _detection(db_session, d1, m(6), is_test=True, p_mosq=None, genus=None)
    db_session.commit()

    return {
        "d1": d1, "d2": d2, "d3": d3,
        "admin": make_user(role=UserRole.ADMIN, cluster_id=c1.id),
        "user": make_user(role=UserRole.USER, cluster_id=c1.id),
        "super": make_user(role=UserRole.SUPER_ADMIN),
    }


def _by_id(body):
    return {d["device_id"]: d for d in body["devices"]}


class TestAccess:
    def test_super_admin_sees_every_device(self, app, client, world):
        _as(app, world["super"])
        res = client.get("/monitoring/data-quality")
        assert res.status_code == 200
        assert set(_by_id(res.json())) == {world["d1"].id, world["d2"].id, world["d3"].id}

    def test_admin_sees_only_own_cluster(self, app, client, world):
        _as(app, world["admin"])
        res = client.get("/monitoring/data-quality")
        assert res.status_code == 200
        assert set(_by_id(res.json())) == {world["d1"].id, world["d3"].id}

    def test_admin_does_not_see_public_clusters(self, app, client, world, make_cluster, make_device):
        make_device(make_cluster(public=True))
        _as(app, world["admin"])
        assert set(_by_id(client.get("/monitoring/data-quality").json())) == {world["d1"].id, world["d3"].id}

    def test_admin_without_cluster_sees_nothing(self, app, client, world, make_user):
        _as(app, make_user(role=UserRole.ADMIN))
        assert client.get("/monitoring/data-quality").json()["devices"] == []

    @pytest.mark.parametrize("role", [UserRole.USER, UserRole.GUEST])
    def test_non_admins_forbidden(self, app, client, world, make_user, role):
        _as(app, make_user(role=role))
        assert client.get("/monitoring/data-quality").status_code == 403

    @pytest.mark.parametrize("days", [0, 91])
    def test_days_bounded(self, app, client, world, days):
        _as(app, world["super"])
        assert client.get(f"/monitoring/data-quality?days={days}").status_code == 422


class TestMetrics:
    @pytest.fixture
    def report(self, app, client, world):
        _as(app, world["super"])
        body = client.get("/monitoring/data-quality").json()
        return body, _by_id(body)

    def test_window_and_thresholds_echoed(self, report):
        body, _ = report
        assert body["days"] == 7
        assert body["offline_threshold_min"] == 10
        assert (body["temp_min"], body["temp_max"]) == (5.0, 45.0)
        assert (body["humidity_min"], body["humidity_max"]) == (10.0, 98.0)

    def test_counts_exclude_test_and_out_of_window_rows(self, report, world):
        d1 = report[1][world["d1"].id]
        assert d1["total_readings"] == 5
        assert d1["test_readings"] == 1

    def test_null_percentages(self, report, world):
        null = report[1][world["d1"].id]["null_pct"]
        assert null["external_temperature"] == 20.0
        assert null["internal_temperature"] == 60.0
        assert null["external_humidity"] == 0.0
        assert null["battery_pct"] == 0.0
        assert null["external_light"] == 100.0
        assert null["esp1_link_alive"] == 100.0

    def test_out_of_range_counts(self, report, world):
        d1 = report[1][world["d1"].id]
        assert d1["temperature_out_of_range"] == 2   # 50 °C external, 2 °C internal fallback
        assert d1["humidity_out_of_range"] == 1      # 99 %
        assert d1["battery_pct_out_of_range"] == 2   # 101 and -1

    def test_silences(self, report, world):
        d1 = report[1][world["d1"].id]
        # 20 and 30 min gaps, plus the ongoing ~120 min since the last reading.
        assert d1["longest_silence_min"] == pytest.approx(120.0, abs=0.5)
        assert d1["silences_over_threshold"] == 3

    def test_detections(self, report, world):
        d1 = report[1][world["d1"].id]
        assert d1["detections"] == 4
        assert d1["detections_missing_p_mosq"] == 2      # null + no reading row
        assert d1["detections_missing_genus"] == 3       # null, blank, no reading row
        assert d1["detections_p_mosq_out_of_range"] == 1

    def test_single_reading_counts_only_the_ongoing_silence(self, report, world):
        d3 = report[1][world["d3"].id]
        assert d3["total_readings"] == 1
        assert d3["longest_silence_min"] == pytest.approx(180.0, abs=0.5)
        assert d3["silences_over_threshold"] == 1
        assert d3["null_pct"]["external_temperature"] == 0.0

    def test_device_without_data_reports_nulls_not_zeros(self, report, world):
        d2 = report[1][world["d2"].id]
        assert d2["total_readings"] == 0
        assert d2["last_reading_at"] is None
        assert set(d2["null_pct"].values()) == {None}
        assert d2["longest_silence_min"] is None
        assert d2["silences_over_threshold"] is None
        assert d2["detections"] == 0



def test_silence_threshold_boundary(app, client, db_session, make_cluster, make_device, make_user):
    """Exactly the threshold is not a silence (strict >, like offline
    detection); just over is. A recent last reading adds no ongoing silence."""
    device = make_device(make_cluster())
    base = datetime.utcnow().replace(microsecond=0) - timedelta(minutes=27)
    for minute in (0, 10, 22, 24, 26):   # gaps 10, 12, 2, 2 min
        _reading(db_session, device, base + timedelta(minutes=minute), external_temperature=20.0)
    db_session.commit()
    _as(app, make_user(role=UserRole.SUPER_ADMIN))
    row = _by_id(client.get("/monitoring/data-quality").json())[device.id]
    assert row["silences_over_threshold"] == 1
    assert row["longest_silence_min"] == pytest.approx(12.0, abs=0.05)


def test_delivery_delay(app, client, db_session, make_cluster, make_device, make_user):
    """Delay = received_at - timestamp over live readings that have
    received_at. NULL rows are not measured (not 0), negative delays (device
    clock ahead) are kept, the threshold is strict >, test rows excluded."""
    cluster = make_cluster()
    device, quiet, old = make_device(cluster), make_device(cluster), make_device(cluster)
    base = datetime.utcnow().replace(microsecond=0) - timedelta(hours=1)
    at = lambda s: base + timedelta(seconds=s)  # noqa: E731
    _reading(db_session, device, base, received_at=at(600))        # exactly 10 min: not over
    _reading(db_session, device, base, received_at=at(601))        # just over
    _reading(db_session, device, base, received_at=at(-30))        # device clock ahead
    _reading(db_session, device, base)                             # not measured
    _reading(db_session, device, base, is_test=True, received_at=at(99_999))
    _reading(db_session, old, base)                                # only pre-feature rows
    db_session.commit()
    _as(app, make_user(role=UserRole.SUPER_ADMIN))
    rows = _by_id(client.get("/monitoring/data-quality").json())

    d = rows[device.id]
    assert d["delay_measured"] == 3
    assert d["avg_delay_s"] == pytest.approx((600 + 601 - 30) / 3, abs=0.1)
    assert (d["min_delay_s"], d["max_delay_s"]) == (-30.0, 601.0)
    assert d["delayed_over_threshold"] == 1
    for r in (rows[quiet.id], rows[old.id]):
        assert r["delay_measured"] == 0 and r["delayed_over_threshold"] == 0
        assert r["avg_delay_s"] is r["min_delay_s"] is r["max_delay_s"] is None
