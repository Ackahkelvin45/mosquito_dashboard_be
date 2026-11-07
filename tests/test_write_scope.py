"""Two write-scope fixes:

1. REST ingest (POST /devices/uuid/{uuid}/sensor-readings | mosquito-events) is
   an admin action on devices the caller MANAGES — not any logged-in user on
   any visible (public) device. Audited.
2. PATCH /auth/users/{id}: only a super admin may edit an ADMIN or SUPER_ADMIN
   account (a cluster admin changing a peer's email + forgot-password = takeover),
   and only a super admin may change ANY user's email (same takeover, on a USER).
"""

import pytest

from app.audit.models import AuditAction, AuditLog
from app.authentication.enums import UserRole
from app.core.database import get_db
from app.device.models import Device

SENSOR = {
    "timestamp": "2026-07-29T16:00:00", "temp_external": 30.0, "temp_internal": 28.0,
    "humidity_external": 70.0, "humidity_internal": 50.0, "pressure_internal": 1010.0,
    "pressure_external": 1009.0, "battery": 3.9,
}
EVENT = {"timestamp": "2026-07-29T16:00:00",
         "mosquito_reading": {"detection_timestamp": "2026-07-29T16:00:00", "genus": "aedes",
                              "age_group": "adult", "sex": "F"}}
ROUTES = [("sensor-readings", SENSOR, AuditAction.SENSOR_READING_INGESTED),
          ("mosquito-events", EVENT, AuditAction.MOSQUITO_EVENT_INGESTED)]


@pytest.fixture
def app(db_session):
    from fastapi import FastAPI
    from app.authentication.routes import router as auth_router
    from app.device.routes import router as device_router

    application = FastAPI()
    application.include_router(auth_router, prefix="/auth")
    application.include_router(device_router, prefix="/devices")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture
def world(make_cluster, make_device):
    own, other, public = make_cluster(), make_cluster(), make_cluster(public=True)
    return {"own": own, "devices": {k: make_device(c) for k, c in
                                    (("own", own), ("other", other), ("public", public))}}


def _post(client, world, key, route, body):
    return client.post(f"/devices/uuid/{world['devices'][key].device_uuid}/{route}", json=body)


class TestIngestScope:
    def test_event_without_sex_or_age_stores_blank_not_500(self, client, login_as, make_user, world, db_session):
        from app.device.models import MosquitoIndividualReading
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        body = {"timestamp": "2026-07-29T16:00:00",
                "mosquito_reading": {"detection_timestamp": "2026-07-29T16:00:00", "genus": "aedes"}}
        assert _post(client, world, "own", "mosquito-events", body).status_code == 201
        row = db_session.query(MosquitoIndividualReading).one()
        assert (row.sex, row.age_group) == ("", "")

    @pytest.mark.parametrize("route,body,_", ROUTES)
    @pytest.mark.parametrize("role", [UserRole.USER, UserRole.GUEST])
    def test_user_and_guest_forbidden(self, client, login_as, make_user, world, route, body, _, role):
        login_as(make_user(role=role, cluster_id=world["own"].id if role == UserRole.USER else None))
        for key in ("own", "public"):
            assert _post(client, world, key, route, body).status_code == 403

    @pytest.mark.parametrize("route,body,action", ROUTES)
    def test_admin_only_on_managed_devices(self, client, login_as, make_user, world,
                                           db_session, route, body, action):
        login_as(make_user(role=UserRole.ADMIN, cluster_id=world["own"].id))
        assert _post(client, world, "own", route, body).status_code == 201
        assert _post(client, world, "other", route, body).status_code == 404
        assert _post(client, world, "public", route, body).status_code == 404
        assert [r.action for r in db_session.query(AuditLog).all()] == [action]

    def test_detection_count_untouched_when_rejected(self, client, login_as, make_user,
                                                     world, db_session):
        login_as(make_user(cluster_id=world["own"].id))
        device = world["devices"]["public"]
        before = device.total_mosquito_count
        _post(client, world, "public", "mosquito-events", EVENT)
        assert db_session.get(Device, device.id).total_mosquito_count == before

    @pytest.mark.parametrize("route,body,_", ROUTES)
    def test_super_admin_anywhere(self, client, login_as, make_user, world, route, body, _):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        for key in ("own", "other", "public"):
            assert _post(client, world, key, route, body).status_code == 201


class TestAdminAccountEdits:
    @pytest.mark.parametrize("target_role", [UserRole.ADMIN, UserRole.SUPER_ADMIN])
    def test_cluster_admin_cannot_edit_admin_accounts(self, client, login_as, make_user,
                                                      make_cluster, db_session, target_role):
        cluster = make_cluster()
        peer = make_user(role=target_role, cluster_id=cluster.id)
        login_as(make_user(role=UserRole.ADMIN, cluster_id=cluster.id))
        for body in ({"email": "takeover@example.com"}, {"is_active": False}, {"role": "USER"}):
            res = client.patch(f"/auth/users/{peer.id}", json=body)
            assert res.status_code == 403, body
            assert "admin account" in res.json()["detail"]
        db_session.refresh(peer)
        assert peer.email != "takeover@example.com" and peer.is_active and peer.role == target_role
        # Still readable.
        assert client.get(f"/auth/users/{peer.id}").status_code == 200

    def test_cluster_admin_cannot_edit_self(self, client, login_as, make_user, make_cluster):
        me = make_user(role=UserRole.ADMIN, cluster_id=make_cluster().id)
        login_as(me)
        assert client.patch(f"/auth/users/{me.id}", json={"is_active": False}).status_code == 403

    def test_cluster_admin_edits_user_in_cluster(self, client, login_as, make_user, make_cluster,
                                                 db_session):
        cluster = make_cluster()
        member = make_user(cluster_id=cluster.id)
        old_email = member.email
        login_as(make_user(role=UserRole.ADMIN, cluster_id=cluster.id))
        # Email changes are super-admin only (new email + forgot-password = takeover)…
        res = client.patch(f"/auth/users/{member.id}", json={"email": "new@example.com", "is_active": False})
        assert res.status_code == 403
        assert res.json()["detail"] == "Only a super admin can change a user's email"
        db_session.refresh(member)
        assert member.email == old_email and member.is_active
        # …other edits work, including re-sending the unchanged email.
        res = client.patch(f"/auth/users/{member.id}", json={"is_active": False})
        assert res.status_code == 200 and res.json()["is_active"] is False
        res = client.patch(f"/auth/users/{member.id}", json={"email": old_email, "first_name": "Yara"})
        assert res.status_code == 200 and res.json()["email"] == old_email
        # Role changes are super-admin only (ADMIN and SUPER_ADMIN alike)…
        for role in ("ADMIN", "SUPER_ADMIN"):
            assert client.patch(f"/auth/users/{member.id}", json={"role": role}).status_code == 403
        # …but re-sending the current role with other edits is fine.
        assert client.patch(f"/auth/users/{member.id}", json={"role": "USER", "first_name": "Xavier"}).status_code == 200

    @pytest.mark.parametrize("target_role", [UserRole.ADMIN, UserRole.SUPER_ADMIN])
    def test_super_admin_edits_admins(self, client, login_as, make_user, make_cluster, target_role):
        target = make_user(role=target_role, cluster_id=make_cluster().id)
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        res = client.patch(f"/auth/users/{target.id}", json={"email": f"moved-{target.id}@example.com"})
        assert res.status_code == 200

    def test_super_admin_changes_user_email(self, client, login_as, make_user, make_cluster):
        member = make_user(cluster_id=make_cluster().id)
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        res = client.patch(f"/auth/users/{member.id}", json={"email": f"renamed-{member.id}@example.com"})
        assert res.status_code == 200
        assert res.json()["email"] == f"renamed-{member.id}@example.com"

    def test_cluster_admin_email_case_variants(self, client, login_as, make_user, make_cluster):
        # Guard compares exactly; EmailStr lowercases only the domain part.
        cluster = make_cluster()
        member = make_user(cluster_id=cluster.id)
        local, domain = member.email.split("@")
        login_as(make_user(role=UserRole.ADMIN, cluster_id=cluster.id))
        url = f"/auth/users/{member.id}"
        assert client.patch(url, json={"email": f"{local}@{domain.upper()}"}).status_code == 200
        assert client.patch(url, json={"email": f"{local.upper()}@{domain}"}).status_code == 403
        assert client.patch(url, json={"email": None}).status_code == 403
