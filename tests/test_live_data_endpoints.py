"""Read endpoints return 200 and never leak test-mode rows (seeded live + test row per table)."""
import time
from datetime import datetime, timedelta
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.authentication.enums import UserRole
from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.device.models import MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading
from utils.protected_route import get_current_user, get_current_user_or_guest


@pytest.fixture
def client(db_session, make_user):
    from app.dashboard.routes import router as dash
    from app.device.routes import router as dev
    from app.mosquito.routes import router as mos
    a = FastAPI()
    a.include_router(dash, prefix="/dashboard")
    a.include_router(dev, prefix="/devices")
    a.include_router(mos, prefix="/mosquito")
    a.dependency_overrides[get_db] = lambda: db_session
    u = UserResponse.model_validate(make_user(role=UserRole.SUPER_ADMIN))
    a.dependency_overrides[get_current_user] = lambda: u
    a.dependency_overrides[get_current_user_or_guest] = lambda: u
    return TestClient(a, raise_server_exceptions=False)


@pytest.fixture
def api(db_session, client, make_device):
    d = make_device(region="R1", community="C1")
    now = datetime.utcnow()
    db_session.add_all([
        SensorDeviceReading(device_id=d.id, timestamp=now - timedelta(hours=1), battery_voltage=3.5, external_temperature=25.0, is_test=False),
        SensorDeviceReading(device_id=d.id, timestamp=now - timedelta(minutes=30), battery_voltage=1.0, external_temperature=40.0, is_test=True),
    ])
    for ts, is_test, cnt, genus in ((now - timedelta(hours=1), False, 1, "aedes"), (now - timedelta(minutes=30), True, 10, "culex")):
        e = MosquitoEvent(device_id=d.id, timestamp=ts, count=cnt, is_test=is_test)
        db_session.add(e); db_session.flush()
        db_session.add(MosquitoIndividualReading(batch_id=e.id, detection_timestamp=ts, genus=genus, species=f"{genus} sp.", sex="female", age_group=""))
    db_session.commit()
    return client, d


def test_endpoints(api):
    c, d = api
    paths = [
        "/dashboard", "/dashboard?totals_group_by=day&chart_group_by=day",
        "/devices/sensor-readings", f"/devices/uuid/{d.device_uuid}/sensor-readings",
        f"/devices/uuid/{d.device_uuid}/mosquito-events", f"/devices/{d.id}/charts",
        "/mosquito", "/mosquito?range=month", "/mosquito/filter-options", "/devices",
    ]
    for p in paths:
        r = c.get(p)
        assert r.status_code == 200, (p, r.text[:500])
        body = r.text.lower()
        assert "culex" not in body, p
        assert "40.0" not in body or p == "/devices", p
    assert c.get("/dashboard").json()["totals"]["total_mosquito_count"] == 1
    assert c.get("/devices/sensor-readings").json()["total"] == 1
    assert c.get("/mosquito").json()["total"] == 1


def test_deleting_test_event_keeps_device_count(db_session, make_device):
    from app.device.repository.device_repository import DeviceRepository
    d = make_device()
    d.total_mosquito_count = 3
    e = MosquitoEvent(device_id=d.id, timestamp=datetime.utcnow(), count=1, is_test=True)
    db_session.add(e); db_session.flush()
    db_session.add(MosquitoIndividualReading(batch_id=e.id, detection_timestamp=e.timestamp,
                                             genus="aedes", sex="female", age_group=""))
    db_session.commit()
    DeviceRepository(db_session).delete_mosquito_event(d.id, e.id)
    db_session.refresh(d)
    assert d.total_mosquito_count == 3


# Dashboard "active" = sent LIVE data in the totals window; test-mode traffic
# doesn't count (but still keeps the device online — see next test).
_NOW = timedelta(0)
@pytest.mark.parametrize("readings,events,active", [
    ([(_NOW, True)], [], 0),                       # test readings only
    ([], [(_NOW, True)], 0),                       # test events only
    ([(_NOW, True), (_NOW, False)], [], 1),        # test + live reading
    ([(_NOW, True), (timedelta(days=3), False)], [], 0),  # live only outside window
])
def test_dashboard_active_counts_live_only(client, db_session, make_device, readings, events, active):
    d = make_device(region="R9", community="C9")
    now = datetime.utcnow() - timedelta(minutes=10)
    for ago, t in readings:
        db_session.add(SensorDeviceReading(device_id=d.id, timestamp=now - ago, external_temperature=25.0, is_test=t))
    for ago, t in events:
        db_session.add(MosquitoEvent(device_id=d.id, timestamp=now - ago, count=3, is_test=t))
    db_session.commit()
    t = client.get("/dashboard?totals_group_by=day&chart_group_by=day").json()["totals"]
    assert (t["active_devices"], t["inactive_devices"], t["total_regions_monitored"]) == (active, 1 - active, active)


def test_test_mode_traffic_keeps_device_online_but_not_active(client, db_session, make_device):
    from app.core.mqtt_client import handle_sensor_data
    d = make_device(last_activity=datetime.utcnow() - timedelta(days=3), region="R9")
    assert client.get("/devices").json()["items"][0]["is_active"] is False
    handle_sensor_data(db_session, d, {"timestamp": int(time.time()), "temp_external": 25.0}, is_test=True)
    assert client.get("/devices").json()["items"][0]["is_active"] is True
    t = client.get("/dashboard?totals_group_by=day&chart_group_by=day").json()["totals"]
    assert (t["active_devices"], t["inactive_devices"]) == (0, 1)
