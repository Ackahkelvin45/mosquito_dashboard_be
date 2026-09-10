"""GET /dashboard cluster scoping — a cluster admin/user must only ever see
totals and chart data for their own cluster plus public clusters, never the
whole fleet, regardless of what cluster_id/region/device_id they pass."""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.authentication.enums import UserRole
from app.core.database import get_db
from app.device.models import MosquitoEvent, SensorDeviceReading
from utils.protected_route import get_current_user, get_current_user_or_guest


@pytest.fixture
def app(db_session):
    from app.dashboard.routes import router as dashboard_router

    application = FastAPI()
    application.include_router(dashboard_router, prefix="/dashboard")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _as(app, user):
    """Override auth to act as `user` for this request (no real JWT needed —
    same override pattern the rest of the suite uses for protected routes)."""
    from app.authentication.schema import UserResponse

    response_user = UserResponse.model_validate(user)
    app.dependency_overrides[get_current_user] = lambda: response_user
    app.dependency_overrides[get_current_user_or_guest] = lambda: response_user


@pytest.fixture
def world(make_cluster, make_user, make_device, db_session):
    """Two private clusters + one public, each with a device carrying real
    mosquito/sensor data — an ADMIN and a USER scoped to c1, a SUPER_ADMIN."""
    c1, c2 = make_cluster(), make_cluster()
    public = make_cluster(public=True)
    d1, d2, dpub = make_device(c1), make_device(c2), make_device(public)

    now = datetime.utcnow()
    for device, count in ((d1, 3), (d2, 7), (dpub, 2)):
        db_session.add(MosquitoEvent(device_id=device.id, timestamp=now, count=count))
        db_session.add(SensorDeviceReading(device_id=device.id, timestamp=now, battery_voltage=3.9))
    db_session.commit()

    return {
        "c1": c1, "c2": c2, "public": public,
        "d1": d1, "d2": d2, "dpub": dpub,
        "admin": make_user(role=UserRole.ADMIN, cluster_id=c1.id),
        "user": make_user(role=UserRole.USER, cluster_id=c1.id),
        "super": make_user(role=UserRole.SUPER_ADMIN),
    }


class TestDashboardScoping:
    def test_admin_sees_only_own_and_public_totals(self, app, client, world):
        _as(app, world["admin"])
        res = client.get("/dashboard").json()
        # Only d1 (3) + dpub (2) — never d2's (7), which belongs to c2.
        assert res["totals"]["total_mosquito_count"] == 5
        assert res["totals"]["total_devices"] == 2

    def test_user_sees_only_own_and_public_totals(self, app, client, world):
        _as(app, world["user"])
        res = client.get("/dashboard").json()
        assert res["totals"]["total_mosquito_count"] == 5
        assert res["totals"]["total_devices"] == 2

    def test_super_admin_sees_everything(self, app, client, world):
        _as(app, world["super"])
        res = client.get("/dashboard").json()
        assert res["totals"]["total_mosquito_count"] == 12
        assert res["totals"]["total_devices"] == 3

    def test_admin_cannot_escape_scope_via_cluster_id_param(self, app, client, world):
        # Explicitly asking for the other admin's cluster must NOT leak it —
        # the backend enforces scope regardless of client-supplied filters.
        _as(app, world["admin"])
        res = client.get(f"/dashboard?cluster_id={world['c2'].id}").json()
        assert res["totals"]["total_mosquito_count"] == 0
        assert res["totals"]["total_devices"] == 0

    def test_super_admin_cluster_filter_narrows_correctly(self, app, client, world):
        _as(app, world["super"])
        res = client.get(f"/dashboard?cluster_id={world['c2'].id}").json()
        assert res["totals"]["total_mosquito_count"] == 7
        assert res["totals"]["total_devices"] == 1

    def test_super_admin_can_select_multiple_clusters(self, app, client, world):
        # cluster_id is repeatable — a super admin combining c1 + c2 must see
        # both (but not the public cluster, since it wasn't asked for).
        _as(app, world["super"])
        res = client.get(
            f"/dashboard?cluster_id={world['c1'].id}&cluster_id={world['c2'].id}"
        ).json()
        assert res["totals"]["total_mosquito_count"] == 10  # 3 + 7
        assert res["totals"]["total_devices"] == 2

    def test_guest_sees_public_only(self, app, client, world):
        # No auth override at all — the real get_current_user_or_guest
        # dependency falls through to its anonymous guest principal.
        res = client.get("/dashboard").json()
        assert res["totals"]["total_mosquito_count"] == 2
        assert res["totals"]["total_devices"] == 1


class TestHourlyActivity:
    """The time-of-day chart: bins detections by detection_timestamp hour,
    excludes test-mode events, and respects cluster scoping."""

    @pytest.fixture
    def hourly_world(self, make_cluster, make_user, make_device, db_session):
        from app.device.models import MosquitoIndividualReading

        cluster = make_cluster()
        device = make_device(cluster)

        def detection(hour, genus, count=1, is_test=False):
            # Yesterday, not today: an hour later than "now" would sit in the
            # future and fall outside the rolling window.
            ts = (datetime.utcnow() - timedelta(days=1)).replace(
                hour=hour, minute=30, second=0, microsecond=0)
            event = MosquitoEvent(device_id=device.id, timestamp=ts,
                                  count=count, is_test=is_test)
            db_session.add(event)
            db_session.flush()
            db_session.add(MosquitoIndividualReading(
                batch_id=event.id, detection_timestamp=ts,
                species=f"{genus} sp.", genus=genus, age_group="", sex="female",
            ))

        detection(18, "aedes", count=2)
        detection(18, "anopheles")
        detection(6, "culex")
        detection(12, "aedes", is_test=True)  # must never appear
        db_session.commit()
        return {"cluster": cluster, "device": device,
                "super": make_user(role=UserRole.SUPER_ADMIN)}

    def test_bins_by_hour_and_excludes_test(self, app, client, hourly_world):
        _as(app, hourly_world["super"])
        body = client.get("/dashboard").json()["hourly_activity"]

        assert len(body["data"]) == 24
        assert [p["hour"] for p in body["data"]] == list(range(24))
        by_hour = {p["hour"]: p for p in body["data"]}
        assert by_hour[18]["total"] == 3
        assert by_hour[18]["by_genus"] == {"aedes": 2, "anopheles": 1, "culex": 0}
        assert by_hour[6]["total"] == 1
        # The test-mode noon detection is excluded entirely.
        assert by_hour[12]["total"] == 0
        assert body["total"] == 4
        assert body["peak_hour"] == 18
        assert body["genera"] == ["aedes", "anopheles", "culex"]

    def test_empty_window_returns_24_zero_bins(self, app, client, world):
        _as(app, world["super"])
        body = client.get("/dashboard?hourly_group_by=day").json()["hourly_activity"]
        # world's events carry no individual readings for "today" hour bins is
        # irrelevant here — just assert the shape contract holds with no data.
        assert len(body["data"]) == 24
        assert body["peak_hour"] is None or isinstance(body["peak_hour"], int)

    def test_cluster_scoping_applies(self, app, client, hourly_world, make_cluster,
                                     make_device, make_user, db_session):
        from app.device.models import MosquitoIndividualReading

        other = make_cluster()
        other_device = make_device(other)
        ts = (datetime.utcnow() - timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0)
        event = MosquitoEvent(device_id=other_device.id, timestamp=ts, count=5)
        db_session.add(event)
        db_session.flush()
        db_session.add(MosquitoIndividualReading(
            batch_id=event.id, detection_timestamp=ts,
            species="culex sp.", genus="culex", age_group="", sex="male",
        ))
        db_session.commit()

        member = make_user(role=UserRole.USER, cluster_id=hourly_world["cluster"].id)
        _as(app, member)
        body = client.get("/dashboard").json()["hourly_activity"]
        by_hour = {p["hour"]: p for p in body["data"]}
        # The other cluster's 9am burst is invisible to this member.
        assert by_hour[9]["total"] == 0
        assert body["total"] == 4
