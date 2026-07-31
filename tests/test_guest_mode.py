"""Guest (anonymous) read-only mode.

A request with NO Authorization header is a GUEST on the public read
endpoints (dashboard, device/cluster reads, historical mosquito data) and
sees ONLY public-cluster data. Everywhere else — every mutation and every
authenticated-only read — an anonymous request must be rejected with 401.

A present-but-invalid token is always 401, never silently downgraded to
guest.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI

from app.core.database import get_db
from app.authentication.enums import UserRole
from app.device.models import MosquitoEvent, MosquitoIndividualReading


@pytest.fixture
def app(db_session):
    """Override conftest's app with the routers guest mode touches."""
    from app.authentication.routes import router as auth_router
    from app.dashboard.routes import router as dashboard_router
    from app.device.routes import router as device_router
    from app.mosquito.routes import router as mosquito_router
    from app.notification.push_routes import router as push_router
    from app.notification.routes import router as notification_router

    application = FastAPI()
    application.include_router(auth_router, prefix="/auth")
    application.include_router(device_router, prefix="/devices")
    application.include_router(mosquito_router, prefix="/mosquito")
    application.include_router(dashboard_router, prefix="/dashboard")
    application.include_router(notification_router, prefix="/notifications")
    application.include_router(push_router, prefix="/push")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture
def make_event(db_session):
    def _make(device, *, species: str = "aegypti", genus: str = "Aedes",
              count: int = 1) -> MosquitoEvent:
        event = MosquitoEvent(
            device_id=device.id,
            timestamp=datetime.utcnow() - timedelta(hours=1),
            count=count,
        )
        db_session.add(event)
        db_session.flush()
        reading = MosquitoIndividualReading(
            batch_id=event.id,
            detection_timestamp=event.timestamp,
            species=species,
            genus=genus,
            age_group="adult",
            sex="female",
        )
        db_session.add(reading)
        db_session.commit()
        db_session.refresh(event)
        return event

    return _make


@pytest.fixture
def public_private_world(make_cluster, make_device, make_event):
    """One public cluster and one private cluster, a device + event in each."""
    public_cluster = make_cluster(public=True)
    private_cluster = make_cluster(public=False)
    public_device = make_device(public_cluster)
    private_device = make_device(private_cluster)
    public_event = make_event(public_device, species="aegypti", genus="Aedes")
    private_event = make_event(private_device, species="gambiae", genus="Anopheles")
    return {
        "public_cluster": public_cluster,
        "private_cluster": private_cluster,
        "public_device": public_device,
        "private_device": private_device,
        "public_event": public_event,
        "private_event": private_event,
    }


class TestGuestReadAccess:
    def test_devices_list_shows_only_public_cluster_devices(self, client,
                                                            public_private_world):
        world = public_private_world
        response = client.get("/devices")
        assert response.status_code == 200
        ids = {d["id"] for d in response.json()["items"]}
        assert ids == {world["public_device"].id}

    def test_device_detail_public_ok_private_404(self, client, public_private_world):
        world = public_private_world
        assert client.get(f"/devices/{world['public_device'].id}").status_code == 200
        # Private devices must 404 (not 403) so their existence isn't revealed.
        assert client.get(f"/devices/{world['private_device'].id}").status_code == 404

    def test_device_by_uuid_scoped_to_public(self, client, public_private_world):
        world = public_private_world
        ok = client.get(f"/devices/uuid/{world['public_device'].device_uuid}")
        assert ok.status_code == 200
        hidden = client.get(f"/devices/uuid/{world['private_device'].device_uuid}")
        assert hidden.status_code == 404

    def test_device_charts_scoped_to_public(self, client, public_private_world):
        world = public_private_world
        assert client.get(f"/devices/{world['public_device'].id}/charts").status_code == 200
        assert client.get(f"/devices/{world['private_device'].id}/charts").status_code == 404

    def test_clusters_list_shows_only_public(self, client, public_private_world):
        world = public_private_world
        response = client.get("/devices/clusters")
        assert response.status_code == 200
        ids = {c["id"] for c in response.json()["items"]}
        assert ids == {world["public_cluster"].id}

    def test_private_cluster_detail_is_404(self, client, public_private_world):
        world = public_private_world
        assert client.get(f"/devices/clusters/{world['public_cluster'].id}").status_code == 200
        assert client.get(f"/devices/clusters/{world['private_cluster'].id}").status_code == 404

    def test_historical_data_shows_only_public_events(self, client,
                                                      public_private_world):
        world = public_private_world
        response = client.get("/mosquito")
        assert response.status_code == 200
        ids = {e["id"] for e in response.json()["items"]}
        assert world["public_event"].id in ids
        assert world["private_event"].id not in ids

    def test_filter_options_hide_private_values(self, client, public_private_world):
        response = client.get("/mosquito/filter-options")
        assert response.status_code == 200
        options = response.json()
        assert "aegypti" in options["species"]
        # Species seen only by the private cluster's device must not leak.
        assert "gambiae" not in options["species"]

    def test_dashboard_counts_only_public_devices(self, client, public_private_world):
        response = client.get("/dashboard")
        assert response.status_code == 200
        totals = response.json()["totals"]
        assert totals["total_devices"] == 1
        assert totals["total_mosquito_count"] == 1

    def test_guest_with_no_public_clusters_sees_empty_world(self, client,
                                                            make_cluster,
                                                            make_device, make_event):
        device = make_device(make_cluster(public=False))
        make_event(device)
        assert client.get("/devices").json()["total"] == 0
        assert client.get("/mosquito").json()["total"] == 0
        assert client.get("/dashboard").json()["totals"]["total_devices"] == 0


WRITE_ATTEMPTS = [
    ("POST", "/devices", {"name": "x", "device_uuid": "x"}),
    ("PATCH", "/devices/1", {"name": "x"}),
    ("DELETE", "/devices/1", None),
    ("POST", "/devices/clusters", {"name": "x"}),
    ("PATCH", "/devices/clusters/1", {"name": "x"}),
    ("DELETE", "/devices/clusters/1", None),
    ("POST", "/devices/uuid/some-uuid/sensor-readings", {}),
    ("POST", "/devices/uuid/some-uuid/mosquito-events", {}),
    ("DELETE", "/devices/uuid/some-uuid/mosquito-events/1", None),
    ("POST", "/auth/register", {"email": "a@example.com", "password": "Password1",
                                "first_name": "Aa", "last_name": "Bb"}),
    ("PATCH", "/notifications/read-all", None),
    ("PATCH", "/notifications/1/read", None),
    ("DELETE", "/notifications/1", None),
    ("PUT", "/notifications/preferences", {"species_alerts": False}),
]

PROTECTED_READS = [
    "/auth/me",
    "/auth/users",
    "/notifications",
    "/notifications/unread-count",
    "/notifications/preferences",
]


class TestGuestCannotWriteOrReachProtected:
    @pytest.mark.parametrize("method,path,body", WRITE_ATTEMPTS,
                             ids=[f"{m} {p}" for m, p, _ in WRITE_ATTEMPTS])
    def test_anonymous_mutation_is_401(self, client, public_private_world,
                                       method, path, body):
        response = client.request(method, path, json=body)
        assert response.status_code == 401, (method, path, response.text)

    @pytest.mark.parametrize("path", PROTECTED_READS)
    def test_anonymous_protected_read_is_401(self, client, path):
        assert client.get(path).status_code == 401, path

    def test_mutations_leave_data_untouched(self, client, db_session,
                                            public_private_world):
        world = public_private_world
        device_id = world["public_device"].id
        client.request("DELETE", f"/devices/{device_id}")
        client.patch(f"/devices/{device_id}", json={"name": "hacked"})
        db_session.expire_all()
        from app.device.models import Device
        device = db_session.get(Device, device_id)
        assert device is not None
        assert device.name != "hacked"


class TestInvalidTokensAreNotGuests:
    """A caller who PRESENTS credentials must present valid ones — a bad
    token on a guest-readable endpoint is 401, not a silent downgrade."""

    @pytest.mark.parametrize("header", [
        "Bearer not-a-real-token",
        "Basic dXNlcjpwYXNz",
        "Bearer ",
        "garbage",
    ])
    def test_bad_authorization_header_is_401(self, client, public_private_world,
                                             header):
        for path in ["/devices", "/mosquito", "/dashboard"]:
            response = client.get(path, headers={"Authorization": header})
            assert response.status_code == 401, (path, header)


class TestAuthenticatedAccessUnchanged:
    def test_member_still_sees_own_cluster_plus_public(self, client, login_as,
                                                       make_user,
                                                       public_private_world):
        world = public_private_world
        member = make_user(role=UserRole.USER,
                           cluster_id=world["private_cluster"].id)
        login_as(member)
        ids = {d["id"] for d in client.get("/devices").json()["items"]}
        assert ids == {world["public_device"].id, world["private_device"].id}

    def test_super_admin_still_sees_everything(self, client, login_as, make_user,
                                               public_private_world):
        world = public_private_world
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        ids = {d["id"] for d in client.get("/devices").json()["items"]}
        assert ids == {world["public_device"].id, world["private_device"].id}
