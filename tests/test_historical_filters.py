"""Historical Data sex / min_confidence filters, and the detector-link flag
(esp1_link_alive) on the device response's latest_reading."""
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
    from app.device.routes import router as dev
    from app.mosquito.routes import router as mos
    a = FastAPI()
    a.include_router(dev, prefix="/devices")
    a.include_router(mos, prefix="/mosquito")
    a.dependency_overrides[get_db] = lambda: db_session
    u = UserResponse.model_validate(make_user(role=UserRole.SUPER_ADMIN))
    a.dependency_overrides[get_current_user] = lambda: u
    a.dependency_overrides[get_current_user_or_guest] = lambda: u
    return TestClient(a, raise_server_exceptions=False)


# (genus, sex, p_mosq) — sex casing as MQTT ("female", "") and REST ("Male", " FEMALE ") may store it.
ROWS = {
    "aedes_f": ("aedes", "female", 0.9),
    "aedes_M": ("aedes", "Male", 0.5),
    "culex_F": ("culex", " FEMALE ", 0.49),
    "culex_blank": ("culex", "", None),
}


@pytest.fixture
def events(db_session, make_device):
    d = make_device()
    now = datetime.utcnow()
    ids = {}
    for i, (key, (genus, sex, p)) in enumerate(ROWS.items()):
        e = MosquitoEvent(device_id=d.id, timestamp=now - timedelta(minutes=i), count=1, is_test=False)
        db_session.add(e); db_session.flush()
        db_session.add(MosquitoIndividualReading(batch_id=e.id, detection_timestamp=e.timestamp,
                                                 genus=genus, sex=sex, age_group="", p_mosq=p))
        ids[e.id] = key
    db_session.commit()
    return ids


def _keys(client, events, qs):
    r = client.get(f"/mosquito?{qs}")
    assert r.status_code == 200, r.text
    return {events[e["id"]] for e in r.json()["items"]}


@pytest.mark.parametrize("qs,expected", [
    ("sex=female", {"aedes_f", "culex_F"}),           # "female" never matches "male" rows, and vice versa
    ("sex=male", {"aedes_M"}),
    ("sex=FEMALE", {"aedes_f", "culex_F"}),           # case-insensitive, trimmed
    ("sex=male&sex=female", {"aedes_f", "aedes_M", "culex_F"}),
    ("sex=female&genus=aedes", {"aedes_f"}),           # AND across filters
    ("sex=male&genus=culex", set()),
])
def test_sex_filter(client, events, qs, expected):
    assert _keys(client, events, qs) == expected


def test_filter_options_offer_normalised_sex(client, events):
    assert client.get("/mosquito/filter-options").json()["sex"] == ["female", "male"]


@pytest.mark.parametrize("qs,expected", [
    ("", set(ROWS)),                                   # not set = everything, NULL included
    ("min_confidence=0", {"aedes_f", "aedes_M", "culex_F"}),  # NULL excluded even at 0
    ("min_confidence=0.5", {"aedes_f", "aedes_M"}),    # inclusive boundary
    ("min_confidence=0.9", {"aedes_f"}),
    ("min_confidence=1", set()),
    ("min_confidence=0.5&sex=male", {"aedes_M"}),
])
def test_min_confidence(client, events, qs, expected):
    assert _keys(client, events, qs) == expected


@pytest.mark.parametrize("v", ["-0.01", "1.01", "abc"])
def test_min_confidence_out_of_range_422(client, events, v):
    assert client.get(f"/mosquito?min_confidence={v}").status_code == 422


@pytest.mark.parametrize("flag", [True, False, None])
def test_device_response_exposes_latest_esp1_link_alive(client, db_session, make_device, flag):
    d = make_device()
    now = datetime.utcnow()
    db_session.add_all([
        # Older reading with the opposite value proves we read the LATEST one.
        SensorDeviceReading(device_id=d.id, timestamp=now - timedelta(hours=1), is_test=False,
                            esp1_link_alive=(not flag) if flag is not None else True),
        SensorDeviceReading(device_id=d.id, timestamp=now, is_test=False, esp1_link_alive=flag),
    ])
    db_session.commit()
    for path in (f"/devices/{d.id}", f"/devices/uuid/{d.device_uuid}"):
        latest = client.get(path).json()["latest_reading"]
        assert "esp1_link_alive" in latest and latest["esp1_link_alive"] is flag, path
    listed = next(x for x in client.get("/devices").json()["items"] if x["id"] == d.id)
    assert listed["latest_reading"]["esp1_link_alive"] is flag


@pytest.mark.parametrize("qs,expected", [
    ("", set(ROWS) | {"bare1", "bare2"}),  # events without readings must survive the no-filter path
    ("sex=female", {"aedes_f", "culex_F"}),
    ("min_confidence=0.5", {"aedes_f", "aedes_M"}),
])
def test_pagination_total_matches_items(client, db_session, events, qs, expected):
    d_id = db_session.get(MosquitoEvent, next(iter(events))).device_id
    now = datetime.utcnow()
    bare = [MosquitoEvent(device_id=d_id, timestamp=now - timedelta(minutes=10 + i), count=0, is_test=False)
            for i in range(2)]
    db_session.add_all(bare); db_session.commit()
    keys = {**events, bare[0].id: "bare1", bare[1].id: "bare2"}
    ids, page = [], 1
    while True:
        body = client.get(f"/mosquito?page_size=2&page={page}&{qs}").json()
        ids += [e["id"] for e in body["items"]]
        if page >= body["total_pages"]:
            break
        page += 1
    assert body["total"] == len(ids) == len(set(ids))
    assert {keys[i] for i in ids} == expected


def test_excluded_event_still_listed_flagged_with_sex_filter(client, db_session, events):
    eid = next(i for i, k in events.items() if k == "aedes_f")
    ev = db_session.get(MosquitoEvent, eid)
    ev.excluded_at, ev.exclusion_reason = datetime.utcnow(), "bad image"
    db_session.commit()
    row = next(e for e in client.get("/mosquito?sex=female").json()["items"] if e["id"] == eid)
    assert row["excluded_at"] is not None and row["exclusion_reason"] == "bad image"
