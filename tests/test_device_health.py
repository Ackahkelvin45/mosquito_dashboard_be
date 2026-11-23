"""GET /monitoring/device-health — scoping, every fault flag on and off,
nulls (never guessed), and the JSON keys the frontend reads."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.authentication.enums import UserRole
from app.core.database import get_db
from app.device.models import SensorDeviceReading
from app.device.schema import OFFLINE_AFTER_MIN, is_online
from app.monitoring.models import MqttIngestError
from app.notification.alert_settings import get_thresholds
from utils.protected_route import get_current_user, get_current_user_or_guest

ALL_FLAGS = {"offline", "never_reported", "low_battery", "detector_disconnected", "no_gps_fix",
             "clock_ahead", "invalid_values", "sensor_missing"}
HEALTHY = dict(external_temperature=25.0, internal_temperature=26.0, external_humidity=60.0,
               internal_humidity=55.0, battery_voltage=4.1, battery_pct=90, esp1_link_alive=True)


@pytest.fixture
def app(db_session):
    from app.monitoring.device_health import router

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


def _reading(db, device, minutes_ago=1, **fields):
    db.add(SensorDeviceReading(device_id=device.id, timestamp=datetime.utcnow() - timedelta(minutes=minutes_ago),
                               **{"is_test": False, **fields}))
    db.commit()


def _rows(client):
    res = client.get("/monitoring/device-health")
    assert res.status_code == 200
    return {d["id"]: d for d in res.json()["devices"]}


def _codes(row):
    return {f["code"] for f in row["faults"]}


@pytest.fixture
def setup(app, db_session, make_cluster, make_device, make_user):
    c1, c2 = make_cluster(), make_cluster()
    healthy = make_device(c1, name="Healthy", latitude=5.6, longitude=-0.17)
    _reading(db_session, healthy, **HEALTHY)
    other = make_device(c2, name="Other")
    _as(app, make_user(role=UserRole.SUPER_ADMIN))
    return {"c1": c1, "healthy": healthy, "other": other,
            "admin": make_user(role=UserRole.ADMIN, cluster_id=c1.id)}


class TestAccess:
    def test_super_admin_sees_all(self, client, setup):
        assert set(_rows(client)) == {setup["healthy"].id, setup["other"].id}

    def test_admin_sees_own_cluster_only(self, app, client, setup):
        _as(app, setup["admin"])
        assert set(_rows(client)) == {setup["healthy"].id}

    def test_admin_without_cluster_sees_nothing(self, app, client, setup, make_user):
        _as(app, make_user(role=UserRole.ADMIN))
        assert _rows(client) == {}

    @pytest.mark.parametrize("role", [UserRole.USER, UserRole.GUEST])
    def test_non_admins_forbidden(self, app, client, setup, make_user, role):
        _as(app, make_user(role=role))
        assert client.get("/monitoring/device-health").status_code == 403


class TestShape:
    def test_json_keys_and_healthy_values(self, client, setup, db_session):
        body = client.get("/monitoring/device-health").json()
        limits = get_thresholds(db_session)
        assert set(body) == {"days", "offline_threshold_min", "battery_low_v", "battery_low_pct", "devices"}
        assert (body["battery_low_v"], body["battery_low_pct"]) == (limits.battery_critical_v, limits.battery_critical_pct)
        row = _rows(client)[setup["healthy"].id]
        assert set(row) == {"id", "device_uuid", "name", "cluster_id", "cluster_name", "region", "last_seen",
                            "last_activity", "is_active", "reading_at", "battery_voltage", "battery_pct", "signal",
                            "signal_reason", "detector_link", "gps_fix", "faults"}
        assert row["is_active"] is True
        assert (row["battery_voltage"], row["battery_pct"]) == (4.1, 90)
        assert row["signal"] is None and row["signal_reason"] == "Devices do not send signal strength yet"
        assert row["detector_link"] is True and row["gps_fix"] is True
        assert row["cluster_name"] == setup["c1"].name
        assert row["faults"] == []

    def test_no_reading_means_nulls(self, client, setup):
        row = _rows(client)[setup["other"].id]
        assert row["battery_voltage"] is None and row["battery_pct"] is None
        assert row["detector_link"] is None and row["gps_fix"] is None
        # No reading: nothing to call missing, no link/gps/battery flags.
        assert _codes(row) == set()

    def test_test_mode_reading_ignored(self, client, setup, db_session):
        _reading(db_session, setup["healthy"], minutes_ago=0, is_test=True, battery_pct=1, esp1_link_alive=False)
        row = _rows(client)[setup["healthy"].id]
        assert row["battery_pct"] == 90 and _codes(row) == set()


class TestFlags:
    @pytest.fixture(autouse=True)
    def _super(self, app, make_user):
        _as(app, make_user(role=UserRole.SUPER_ADMIN))

    def test_offline(self, client, make_device, db_session):
        d = make_device(last_activity=datetime.utcnow() - timedelta(hours=2))
        _reading(db_session, d, minutes_ago=120, **HEALTHY)
        row = _rows(client)[d.id]
        assert row["is_active"] is False and _codes(row) == {"offline"}

    def test_never_reported(self, client, make_device):
        d = make_device(last_sensor_data_at=None)
        row = _rows(client)[d.id]
        assert row["last_seen"] is None and _codes(row) == {"never_reported"}

    @pytest.mark.parametrize("fields,low", [
        ({"battery_pct": 5, "battery_voltage": 4.2}, True),      # pct wins over a healthy voltage
        ({"battery_pct": 90, "battery_voltage": 0.1}, False),
        ({"battery_pct": None, "battery_voltage": 0.1}, True),   # voltage only without pct
        ({"battery_pct": None, "battery_voltage": None}, False),
    ])
    def test_low_battery(self, client, make_device, db_session, fields, low):
        d = make_device()
        _reading(db_session, d, **{**HEALTHY, **fields})
        assert ("low_battery" in _codes(_rows(client)[d.id])) is low

    @pytest.mark.parametrize("link,flagged", [(False, True), (True, False), (None, False)])
    def test_detector_disconnected(self, client, make_device, db_session, link, flagged):
        d = make_device()
        _reading(db_session, d, **{**HEALTHY, "esp1_link_alive": link})
        row = _rows(client)[d.id]
        assert row["detector_link"] is link
        assert ("detector_disconnected" in _codes(row)) is flagged

    @pytest.mark.parametrize("lat,lon,fix", [(0.0, 0.0, False), (5.6, -0.17, True), (None, None, None)])
    def test_gps_fix(self, client, make_device, db_session, lat, lon, fix):
        d = make_device(latitude=lat, longitude=lon)
        _reading(db_session, d, **HEALTHY)
        row = _rows(client)[d.id]
        assert row["gps_fix"] is fix
        assert ("no_gps_fix" in _codes(row)) is (fix is False)

    def test_clock_ahead(self, client, make_device, db_session):
        d = make_device()
        now = datetime.utcnow()
        _reading(db_session, d, **HEALTHY)
        assert "clock_ahead" not in _codes(_rows(client)[d.id])
        db_session.add(SensorDeviceReading(device_id=d.id, timestamp=now + timedelta(hours=1),
                                           received_at=now - timedelta(minutes=1), is_test=False, **HEALTHY))
        db_session.commit()
        assert "clock_ahead" in _codes(_rows(client)[d.id])

    def test_invalid_values(self, client, make_device, db_session):
        d = make_device()
        _reading(db_session, d, **HEALTHY)
        db_session.add(MqttIngestError(error_type="invalid_value", device_uuid=d.device_uuid, detail="battery_pct=255",
                                       occurred_at=datetime.utcnow() - timedelta(days=8)))
        db_session.commit()
        assert "invalid_values" not in _codes(_rows(client)[d.id])  # outside 7 days
        db_session.add(MqttIngestError(error_type="invalid_value", device_uuid=d.device_uuid, detail="battery_pct=255",
                                       occurred_at=datetime.utcnow() - timedelta(hours=1)))
        db_session.commit()
        assert "invalid_values" in _codes(_rows(client)[d.id])

    def test_sensor_missing(self, client, make_device, db_session):
        no_humidity = {**HEALTHY, "external_humidity": None, "internal_humidity": None}
        d = make_device()
        _reading(db_session, d, minutes_ago=3, **no_humidity)
        _reading(db_session, d, minutes_ago=2, **{**no_humidity, "internal_humidity": 50.0})
        assert "sensor_missing" not in _codes(_rows(client)[d.id])  # one value is enough
        e = make_device()
        _reading(db_session, e, **no_humidity)
        fault = next(f for f in _rows(client)[e.id]["faults"] if f["code"] == "sensor_missing")
        assert "humidity" in fault["detail"] and "temperature" not in fault["detail"]

    def test_sensor_missing_needs_both_sensors(self, client, make_device, db_session):
        # A build without an external sensor: internal still reports, not flagged.
        d = make_device()
        _reading(db_session, d, **{**HEALTHY, "external_temperature": None, "external_humidity": None})
        assert "sensor_missing" not in _codes(_rows(client)[d.id])

    def test_sensor_missing_ignores_csv_imports(self, client, make_device, make_user, db_session):
        from app.device.models import DataImport
        d = make_device()
        batch = DataImport(kind="sensor-readings", rows_total=1, rows_imported=1, rows_skipped=0,
                           imported_by_user_id=make_user().id)
        db_session.add(batch)
        db_session.commit()
        _reading(db_session, d, minutes_ago=5, import_batch_id=batch.id, **HEALTHY)  # imported values
        _reading(db_session, d, **{**HEALTHY, "external_temperature": None, "internal_temperature": None})
        assert "sensor_missing" in _codes(_rows(client)[d.id])

    def test_future_dated_reading_does_not_become_latest(self, client, make_device, db_session):
        now = datetime.utcnow()
        d = make_device()
        _reading(db_session, d, minutes_ago=2, **HEALTHY)
        db_session.add(SensorDeviceReading(device_id=d.id, timestamp=now + timedelta(days=365), received_at=now,
                                           is_test=False, **{**HEALTHY, "battery_pct": 1, "esp1_link_alive": False}))
        db_session.commit()
        row = _rows(client)[d.id]
        assert (row["battery_pct"], row["detector_link"]) == (90, True)
        assert row["reading_at"] < now.isoformat()
        assert _codes(row) == {"clock_ahead"}

    def test_battery_pct_out_of_range_is_not_a_reading(self, client, make_device, db_session):
        d = make_device()
        _reading(db_session, d, **{**HEALTHY, "battery_pct": -5})
        row = _rows(client)[d.id]
        assert row["battery_pct"] is None and row["battery_voltage"] == 4.1
        assert "low_battery" not in _codes(row)

    def test_every_flag_at_once(self, client, make_device, db_session):
        now = datetime.utcnow()
        d = make_device(last_activity=now - timedelta(hours=1), latitude=0.0, longitude=0.0)
        # Latest real reading: low battery, detector down, no temperature/humidity at all.
        _reading(db_session, d, minutes_ago=90, battery_pct=1, esp1_link_alive=False)
        # Future-stamped row: clock ahead only.
        db_session.add(SensorDeviceReading(device_id=d.id, timestamp=now + timedelta(hours=1),
                                           received_at=now - timedelta(hours=1), is_test=False, **HEALTHY))
        db_session.add(MqttIngestError(error_type="invalid_value", device_uuid=d.device_uuid, occurred_at=now))
        db_session.commit()
        codes = _codes(_rows(client)[d.id])
        assert codes == ALL_FLAGS - {"never_reported"}
        assert all(f["label"] and f["detail"] for f in _rows(client)[d.id]["faults"])

    def test_device_id_param(self, client, setup):
        res = client.get(f"/monitoring/device-health?device_id={setup['healthy'].id}").json()
        assert [d["id"] for d in res["devices"]] == [setup["healthy"].id]

    def test_device_id_outside_scope_is_empty(self, app, client, setup):
        _as(app, setup["admin"])
        assert client.get(f"/monitoring/device-health?device_id={setup['other'].id}").json()["devices"] == []

    def test_sensor_missing_uses_exact_count_not_rounded_pct(self, client, make_device, db_session):
        # 1 value in 10,001 readings rounds to null_pct 100.0 but the sensor is not missing.
        d = make_device()
        now = datetime.utcnow()
        db_session.bulk_insert_mappings(SensorDeviceReading, [
            {"device_id": d.id, "timestamp": now - timedelta(seconds=i + 60), "is_test": False,
             **{**HEALTHY, "external_humidity": None, "internal_humidity": None}} for i in range(10_000)])
        _reading(db_session, d, **HEALTHY)
        assert "sensor_missing" not in _codes(_rows(client)[d.id])


class TestIsOnline:
    @pytest.mark.parametrize("tz", [None, timezone.utc])
    def test_threshold_boundary(self, tz):
        now = datetime.now(timezone.utc).replace(tzinfo=tz)
        assert is_online(now - timedelta(minutes=OFFLINE_AFTER_MIN, seconds=-30)) is True
        assert is_online(now - timedelta(minutes=OFFLINE_AFTER_MIN, seconds=30)) is False
        assert is_online(None) is False
