"""Public data API (/api/v1): key auth, cluster scoping, filters, cursor
pagination, field selection, exports, rate limiting."""
import csv
import io
import json
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api_access.auth as api_auth
from app.api_access.models import ApiKey
from app.authentication.enums import UserRole
from app.core.database import get_db
from app.device.models import MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading


@pytest.fixture
def app(db_session):
    from app.api_access.public_routes import router as public_router

    application = FastAPI()
    application.include_router(public_router, prefix="/api/v1")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def clean_rate_limiter():
    # The limiter is process-global and user ids restart at 1 every test —
    # without this, requests from different tests share one minute-window.
    api_auth._windows.clear()
    yield
    api_auth._windows.clear()


@pytest.fixture(autouse=True)
def patch_stream_session(monkeypatch, TestingSessionLocal):
    # The export generator opens its own SessionLocal (the request session is
    # closed before a StreamingResponse body runs); point it at the test engine.
    import app.api_access.public_routes as public_routes

    monkeypatch.setattr(public_routes, "SessionLocal", TestingSessionLocal)


@pytest.fixture
def make_api_key(db_session):
    def _make(user, *, expires_at=None, revoked_at=None) -> tuple[str, ApiKey]:
        raw, prefix, key_hash = ApiKey.generate()
        key = ApiKey(
            name="test key", key_prefix=prefix, key_hash=key_hash,
            user_id=user.id, expires_at=expires_at, revoked_at=revoked_at,
        )
        db_session.add(key)
        db_session.commit()
        db_session.refresh(key)
        return raw, key

    return _make


@pytest.fixture
def make_reading(db_session):
    def _make(device, *, timestamp=None, **overrides) -> SensorDeviceReading:
        reading = SensorDeviceReading(
            device_id=device.id,
            timestamp=timestamp or datetime.utcnow(),
            external_temperature=overrides.pop("external_temperature", 30.0),
            **overrides,
        )
        db_session.add(reading)
        db_session.commit()
        return reading

    return _make


@pytest.fixture
def make_event(db_session):
    def _make(device, *, timestamp=None, species="Aedes aegypti", count=1) -> MosquitoEvent:
        event = MosquitoEvent(device_id=device.id, timestamp=timestamp or datetime.utcnow(), count=count)
        db_session.add(event)
        db_session.flush()
        db_session.add(MosquitoIndividualReading(
            batch_id=event.id, detection_timestamp=event.timestamp,
            species=species, genus="Aedes", age_group="adult", sex="F",
        ))
        db_session.commit()
        return event

    return _make


@pytest.fixture
def world(make_cluster, make_user, make_device):
    """Two private clusters + one public; a USER and an ADMIN in c1, a SUPER_ADMIN."""
    c1, c2 = make_cluster(), make_cluster()
    public = make_cluster(public=True)
    return {
        "c1": c1, "c2": c2, "public": public,
        "user": make_user(cluster_id=c1.id),
        "admin": make_user(role=UserRole.ADMIN, cluster_id=c1.id),
        "super": make_user(role=UserRole.SUPER_ADMIN),
        "d1": make_device(c1), "d2": make_device(c2), "dpub": make_device(public),
    }


def auth(raw_key: str) -> dict:
    return {"X-API-Key": raw_key}


# ── Authentication ───────────────────────────────────────────────────────────

class TestKeyAuth:
    def test_missing_header(self, client):
        assert client.get("/api/v1/devices").status_code == 401

    def test_malformed_key(self, client):
        assert client.get("/api/v1/devices", headers=auth("not-a-key")).status_code == 401

    def test_unknown_key(self, client):
        assert client.get("/api/v1/devices", headers=auth("mk_" + "a" * 43)).status_code == 401

    def test_valid_key(self, client, world, make_api_key):
        raw, _ = make_api_key(world["user"])
        assert client.get("/api/v1/devices", headers=auth(raw)).status_code == 200

    def test_revoked_key(self, client, world, make_api_key):
        raw, _ = make_api_key(world["user"], revoked_at=datetime.utcnow())
        assert client.get("/api/v1/devices", headers=auth(raw)).status_code == 401

    def test_expired_key(self, client, world, make_api_key):
        raw, _ = make_api_key(world["user"], expires_at=datetime.utcnow() - timedelta(seconds=1))
        assert client.get("/api/v1/devices", headers=auth(raw)).status_code == 401

    def test_expiry_boundary_is_exclusive(self, client, world, make_api_key):
        # expires_at in the future by a comfortable margin → still valid.
        raw, _ = make_api_key(world["user"], expires_at=datetime.utcnow() + timedelta(hours=1))
        assert client.get("/api/v1/devices", headers=auth(raw)).status_code == 200

    def test_inactive_owner(self, client, world, make_api_key, db_session):
        raw, _ = make_api_key(world["user"])
        world["user"].is_active = False
        db_session.commit()
        assert client.get("/api/v1/devices", headers=auth(raw)).status_code == 401

    def test_unapproved_owner(self, client, world, make_api_key, db_session):
        from app.authentication.enums import ApprovalStatus

        raw, _ = make_api_key(world["user"])
        world["user"].approval_status = ApprovalStatus.REJECTED
        db_session.commit()
        assert client.get("/api/v1/devices", headers=auth(raw)).status_code == 401

    def test_usage_is_tracked(self, client, world, make_api_key, db_session):
        raw, key = make_api_key(world["user"])
        assert key.last_used_at is None and key.total_requests == 0
        client.get("/api/v1/devices", headers=auth(raw))
        client.get("/api/v1/devices", headers=auth(raw))
        db_session.refresh(key)
        assert key.total_requests == 2
        assert key.last_used_at is not None


# ── Scoping / data isolation ─────────────────────────────────────────────────

class TestScoping:
    def test_user_sees_own_and_public_devices_only(self, client, world, make_api_key):
        raw, _ = make_api_key(world["user"])
        uuids = {d["device_uuid"] for d in client.get("/api/v1/devices", headers=auth(raw)).json()["items"]}
        assert uuids == {world["d1"].device_uuid, world["dpub"].device_uuid}

    def test_super_admin_sees_everything(self, client, world, make_api_key):
        raw, _ = make_api_key(world["super"])
        res = client.get("/api/v1/devices", headers=auth(raw)).json()
        assert res["total"] == 3

    def test_out_of_scope_cluster_filter_is_empty(self, client, world, make_api_key):
        raw, _ = make_api_key(world["user"])
        res = client.get(f"/api/v1/devices?cluster_id={world['c2'].id}", headers=auth(raw)).json()
        assert res["items"] == [] and res["total"] == 0

    def test_mixed_cluster_filter_intersects(self, client, world, make_api_key):
        # Asking for "mine + someone else's" must return only mine — never
        # fall back to full scope, never include the other cluster.
        raw, _ = make_api_key(world["user"])
        res = client.get(
            f"/api/v1/devices?cluster_id={world['c1'].id}&cluster_id={world['c2'].id}",
            headers=auth(raw),
        ).json()
        uuids = {d["device_uuid"] for d in res["items"]}
        assert uuids == {world["d1"].device_uuid}

    def test_sensor_readings_isolated(self, client, world, make_api_key, make_reading):
        make_reading(world["d1"])
        make_reading(world["d2"])
        raw, _ = make_api_key(world["user"])
        items = client.get("/api/v1/sensor-readings", headers=auth(raw)).json()["items"]
        assert {r["device_uuid"] for r in items} == {world["d1"].device_uuid}

    def test_events_isolated(self, client, world, make_api_key, make_event):
        make_event(world["d1"], species="mine")
        make_event(world["d2"], species="theirs")
        raw, _ = make_api_key(world["user"])
        items = client.get("/api/v1/mosquito-events", headers=auth(raw)).json()["items"]
        assert {e["species"] for e in items} == {"mine"}

    def test_clusters_listing_scoped(self, client, world, make_api_key):
        raw, _ = make_api_key(world["user"])
        ids = {c["id"] for c in client.get("/api/v1/clusters", headers=auth(raw)).json()["items"]}
        assert ids == {world["c1"].id, world["public"].id}

    # An ADMIN's key follows the exact same visible_cluster_ids() rule as a
    # USER's — own cluster ∪ public — never their whole-fleet management
    # reach. This is the case the "cluster admin only sees his cluster's
    # data" requirement is actually about.
    def test_admin_key_sees_own_and_public_only(
        self, client, world, make_api_key, make_reading, make_event
    ):
        make_reading(world["d1"])
        make_reading(world["d2"])  # a different admin's cluster — must never leak
        make_event(world["d1"], species="mine")
        make_event(world["d2"], species="not-mine")
        raw, _ = make_api_key(world["admin"])

        device_uuids = {
            d["device_uuid"] for d in client.get("/api/v1/devices", headers=auth(raw)).json()["items"]
        }
        assert device_uuids == {world["d1"].device_uuid, world["dpub"].device_uuid}

        readings = client.get("/api/v1/sensor-readings", headers=auth(raw)).json()["items"]
        assert {r["device_uuid"] for r in readings} == {world["d1"].device_uuid}

        events = client.get("/api/v1/mosquito-events", headers=auth(raw)).json()["items"]
        assert {e["species"] for e in events} == {"mine"}

        cluster_ids = {c["id"] for c in client.get("/api/v1/clusters", headers=auth(raw)).json()["items"]}
        assert cluster_ids == {world["c1"].id, world["public"].id}

    def test_admin_key_cannot_reach_another_clusters_data_via_filter(
        self, client, world, make_api_key
    ):
        # Explicitly asking for another admin's cluster must still yield
        # nothing — an admin's key is not a backdoor around the intersection.
        raw, _ = make_api_key(world["admin"])
        res = client.get(f"/api/v1/devices?cluster_id={world['c2'].id}", headers=auth(raw)).json()
        assert res["items"] == [] and res["total"] == 0

    def test_admin_role_change_narrows_key_scope_next_request(
        self, client, world, make_api_key, db_session
    ):
        # The design contract: a key's scope is resolved fresh every request
        # from the OWNER's current role/cluster, never cached on the key
        # itself. If an admin is demoted or reassigned, their key must
        # reflect that on the very next call, not after re-issuing it.
        raw, _ = make_api_key(world["admin"])
        before = {d["device_uuid"] for d in client.get("/api/v1/devices", headers=auth(raw)).json()["items"]}
        assert world["d1"].device_uuid in before

        world["admin"].cluster_id = world["c2"].id
        db_session.commit()

        after = {d["device_uuid"] for d in client.get("/api/v1/devices", headers=auth(raw)).json()["items"]}
        assert world["d1"].device_uuid not in after
        assert world["d2"].device_uuid in after


# ── Filtering / sorting / pagination / fields ────────────────────────────────

class TestQueries:
    def test_date_range(self, client, world, make_api_key, make_reading):
        old = make_reading(world["d1"], timestamp=datetime(2026, 1, 1))
        new = make_reading(world["d1"], timestamp=datetime(2026, 6, 1))
        raw, _ = make_api_key(world["user"])
        items = client.get(
            "/api/v1/sensor-readings?start_date=2026-05-01T00:00:00&end_date=2026-07-01T00:00:00",
            headers=auth(raw),
        ).json()["items"]
        assert [r["id"] for r in items] == [new.id]
        assert items[0]["timestamp"].endswith("Z")

    def test_device_uuid_filter(self, client, world, make_api_key, make_reading):
        make_reading(world["d1"])
        make_reading(world["dpub"])
        raw, _ = make_api_key(world["user"])
        items = client.get(
            f"/api/v1/sensor-readings?device_uuid={world['d1'].device_uuid}", headers=auth(raw)
        ).json()["items"]
        assert {r["device_uuid"] for r in items} == {world["d1"].device_uuid}

    def test_species_filter(self, client, world, make_api_key, make_event):
        make_event(world["d1"], species="Aedes aegypti")
        make_event(world["d1"], species="Culex pipiens")
        raw, _ = make_api_key(world["user"])
        items = client.get("/api/v1/mosquito-events?species=aedes", headers=auth(raw)).json()["items"]
        assert [e["species"] for e in items] == ["Aedes aegypti"]

    def test_cursor_walk_is_complete_and_duplicate_free(self, client, world, make_api_key, make_reading):
        base = datetime(2026, 3, 1)
        expected = [make_reading(world["d1"], timestamp=base + timedelta(minutes=i)).id for i in range(5)]
        raw, _ = make_api_key(world["user"])

        seen, cursor, pages = [], None, 0
        while True:
            url = "/api/v1/sensor-readings?limit=2&order=asc" + (f"&cursor={cursor}" if cursor else "")
            body = client.get(url, headers=auth(raw)).json()
            seen += [r["id"] for r in body["items"]]
            pages += 1
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert seen == expected
        assert pages == 3

    def test_order_desc_default(self, client, world, make_api_key, make_reading):
        base = datetime(2026, 3, 1)
        ids = [make_reading(world["d1"], timestamp=base + timedelta(minutes=i)).id for i in range(3)]
        raw, _ = make_api_key(world["user"])
        items = client.get("/api/v1/sensor-readings", headers=auth(raw)).json()["items"]
        assert [r["id"] for r in items] == list(reversed(ids))

    def test_field_selection(self, client, world, make_api_key, make_reading):
        make_reading(world["d1"])
        raw, _ = make_api_key(world["user"])
        items = client.get(
            "/api/v1/sensor-readings?fields=timestamp,external_temperature", headers=auth(raw)
        ).json()["items"]
        assert set(items[0].keys()) == {"timestamp", "external_temperature"}

    def test_unknown_field_422(self, client, world, make_api_key):
        raw, _ = make_api_key(world["user"])
        assert client.get("/api/v1/sensor-readings?fields=password", headers=auth(raw)).status_code == 422

    def test_invalid_cursor_422(self, client, world, make_api_key):
        raw, _ = make_api_key(world["user"])
        assert client.get("/api/v1/sensor-readings?cursor=garbage!", headers=auth(raw)).status_code == 422

    def test_limit_cap_422(self, client, world, make_api_key):
        raw, _ = make_api_key(world["user"])
        assert client.get("/api/v1/sensor-readings?limit=501", headers=auth(raw)).status_code == 422

    def test_empty_result(self, client, world, make_api_key):
        raw, _ = make_api_key(world["user"])
        body = client.get("/api/v1/sensor-readings", headers=auth(raw)).json()
        assert body == {"items": [], "next_cursor": None}

    def test_devices_sort_and_page_past_end(self, client, world, make_api_key):
        raw, _ = make_api_key(world["super"])
        body = client.get("/api/v1/devices?sort=name&order=asc&page=99", headers=auth(raw)).json()
        assert body["items"] == [] and body["total"] == 3


# ── Export ───────────────────────────────────────────────────────────────────

class TestExport:
    def test_csv_export(self, client, world, make_api_key, make_reading):
        make_reading(world["d1"], external_temperature=31.5)
        make_reading(world["d2"], external_temperature=99.0)  # out of scope
        raw, _ = make_api_key(world["user"])
        res = client.get("/api/v1/export?dataset=sensor-readings&format=csv", headers=auth(raw))
        assert res.status_code == 200
        assert "attachment" in res.headers["content-disposition"]
        rows = list(csv.reader(io.StringIO(res.text)))
        header, data = rows[0], rows[1:]
        assert header[0] == "id" and "external_temperature" in header
        assert len(data) == 1
        assert data[0][header.index("external_temperature")] == "31.5"

    def test_jsonl_export(self, client, world, make_api_key, make_event):
        make_event(world["d1"], species="Aedes aegypti")
        raw, _ = make_api_key(world["user"])
        res = client.get("/api/v1/export?dataset=mosquito-events&format=jsonl", headers=auth(raw))
        lines = [json.loads(l) for l in res.text.strip().splitlines()]
        assert len(lines) == 1
        assert lines[0]["species"] == "Aedes aegypti"
        assert lines[0]["timestamp"].endswith("Z")

    def test_csv_formula_injection_guarded(self, client, world, make_api_key, make_event):
        make_event(world["d1"], species="=SUM(A1:A9)")
        raw, _ = make_api_key(world["user"])
        res = client.get("/api/v1/export?dataset=mosquito-events&format=csv", headers=auth(raw))
        rows = list(csv.reader(io.StringIO(res.text)))
        species = rows[1][rows[0].index("species")]
        assert species.startswith("'=")

    def test_export_respects_filters(self, client, world, make_api_key, make_reading):
        make_reading(world["d1"], timestamp=datetime(2026, 1, 1))
        make_reading(world["d1"], timestamp=datetime(2026, 6, 1))
        raw, _ = make_api_key(world["user"])
        res = client.get(
            "/api/v1/export?dataset=sensor-readings&start_date=2026-05-01T00:00:00", headers=auth(raw)
        )
        assert len(res.text.strip().splitlines()) == 2  # header + 1 row

    def test_invalid_dataset_422(self, client, world, make_api_key):
        raw, _ = make_api_key(world["user"])
        assert client.get("/api/v1/export?dataset=users", headers=auth(raw)).status_code == 422


# ── Rate limiting ────────────────────────────────────────────────────────────

class TestRateLimit:
    def test_data_limit_429(self, client, world, make_api_key, monkeypatch):
        monkeypatch.setattr(api_auth, "DATA_RATE_LIMIT_PER_MIN", 3)
        raw, _ = make_api_key(world["user"])
        for _ in range(3):
            assert client.get("/api/v1/devices", headers=auth(raw)).status_code == 200
        res = client.get("/api/v1/devices", headers=auth(raw))
        assert res.status_code == 429
        assert "retry-after" in res.headers

    def test_limit_is_per_owner_not_per_key(self, client, world, make_api_key, monkeypatch):
        # Minting a second key must NOT double the budget.
        monkeypatch.setattr(api_auth, "DATA_RATE_LIMIT_PER_MIN", 3)
        raw1, _ = make_api_key(world["user"])
        raw2, _ = make_api_key(world["user"])
        for _ in range(3):
            assert client.get("/api/v1/devices", headers=auth(raw1)).status_code == 200
        assert client.get("/api/v1/devices", headers=auth(raw2)).status_code == 429

    def test_export_limit_separate_bucket(self, client, world, make_api_key, monkeypatch):
        monkeypatch.setattr(api_auth, "EXPORT_RATE_LIMIT_PER_MIN", 1)
        raw, _ = make_api_key(world["user"])
        assert client.get("/api/v1/export?dataset=devices", headers=auth(raw)).status_code == 200
        assert client.get("/api/v1/export?dataset=devices", headers=auth(raw)).status_code == 429
        # Query bucket unaffected by the exhausted export bucket.
        assert client.get("/api/v1/devices", headers=auth(raw)).status_code == 200
