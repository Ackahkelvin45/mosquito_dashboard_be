"""API key lifecycle + permissions for the /api-keys management endpoints."""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api_access.models import ApiKey
from app.authentication.enums import ApprovalStatus, UserRole
from app.core.database import get_db


@pytest.fixture
def app(db_session):
    from app.api_access.routes import router as api_keys_router

    application = FastAPI()
    application.include_router(api_keys_router, prefix="/api-keys")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


class TestCreate:
    @pytest.mark.parametrize("role", [UserRole.USER, UserRole.ADMIN, UserRole.SUPER_ADMIN])
    def test_each_role_can_create(self, client, login_as, make_user, role):
        login_as(make_user(role=role))
        res = client.post("/api-keys", json={"name": "research key"})
        assert res.status_code == 201
        body = res.json()
        assert body["api_key"].startswith("mk_")
        assert body["key_prefix"] == body["api_key"][:12]
        assert body["name"] == "research key"
        assert body["revoked_at"] is None

    def test_anonymous_cannot_create(self, client):
        res = client.post("/api-keys", json={"name": "nope"})
        assert res.status_code == 401

    def test_inactive_user_cannot_create(self, client, login_as, make_user):
        login_as(make_user(is_active=False))
        assert client.post("/api-keys", json={"name": "nope"}).status_code == 403

    def test_unapproved_user_cannot_create(self, client, login_as, make_user, db_session):
        user = make_user()
        user.approval_status = ApprovalStatus.PENDING
        db_session.commit()
        login_as(user)
        assert client.post("/api-keys", json={"name": "nope"}).status_code == 403

    def test_expiry_is_set(self, client, login_as, make_user):
        login_as(make_user())
        res = client.post("/api-keys", json={"name": "temp key", "expires_in_days": 30})
        assert res.status_code == 201
        expires = datetime.fromisoformat(res.json()["expires_at"])
        assert abs((expires - (datetime.utcnow() + timedelta(days=30))).total_seconds()) < 60

    def test_validation(self, client, login_as, make_user):
        login_as(make_user())
        assert client.post("/api-keys", json={"name": "x"}).status_code == 422  # too short
        assert client.post("/api-keys", json={"name": "ok", "expires_in_days": 0}).status_code == 422

    def test_raw_key_never_stored(self, client, login_as, make_user, db_session):
        login_as(make_user())
        raw = client.post("/api-keys", json={"name": "secret"}).json()["api_key"]
        stored = db_session.query(ApiKey).one()
        assert raw not in (stored.key_hash, stored.key_prefix)
        assert stored.key_hash == ApiKey.hash_key(raw)


class TestList:
    def test_lists_only_own_keys(self, client, login_as, make_user):
        alice, bob = make_user(), make_user()
        login_as(alice)
        client.post("/api-keys", json={"name": "alice key"})
        login_as(bob)
        client.post("/api-keys", json={"name": "bob key"})

        names = [k["name"] for k in client.get("/api-keys").json()]
        assert names == ["bob key"]

    def test_list_never_contains_secrets(self, client, login_as, make_user):
        login_as(make_user())
        client.post("/api-keys", json={"name": "secret"})
        listed = client.get("/api-keys").json()[0]
        assert "api_key" not in listed
        assert "key_hash" not in listed

    def test_all_requires_super_admin(self, client, login_as, make_user):
        for role in (UserRole.USER, UserRole.ADMIN):
            login_as(make_user(role=role))
            assert client.get("/api-keys?all=true").status_code == 403

    def test_super_admin_sees_all_with_owner(self, client, login_as, make_user):
        user = make_user()
        login_as(user)
        client.post("/api-keys", json={"name": "user key"})
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        client.post("/api-keys", json={"name": "admin key"})

        keys = client.get("/api-keys?all=true").json()
        assert {k["name"] for k in keys} == {"user key", "admin key"}
        assert user.email in {k["owner_email"] for k in keys}

    def test_revoked_keys_stay_listed(self, client, login_as, make_user):
        login_as(make_user())
        key_id = client.post("/api-keys", json={"name": "doomed"}).json()["id"]
        client.delete(f"/api-keys/{key_id}")
        listed = client.get("/api-keys").json()
        assert len(listed) == 1
        assert listed[0]["revoked_at"] is not None


class TestRevoke:
    def test_owner_can_revoke(self, client, login_as, make_user):
        login_as(make_user())
        key_id = client.post("/api-keys", json={"name": "key"}).json()["id"]
        assert client.delete(f"/api-keys/{key_id}").status_code == 204

    def test_double_revoke_is_idempotent(self, client, login_as, make_user):
        login_as(make_user())
        key_id = client.post("/api-keys", json={"name": "key"}).json()["id"]
        assert client.delete(f"/api-keys/{key_id}").status_code == 204
        assert client.delete(f"/api-keys/{key_id}").status_code == 204

    def test_non_owner_gets_404(self, client, login_as, make_user):
        login_as(make_user())
        key_id = client.post("/api-keys", json={"name": "key"}).json()["id"]
        for role in (UserRole.USER, UserRole.ADMIN):
            login_as(make_user(role=role))
            assert client.delete(f"/api-keys/{key_id}").status_code == 404

    def test_super_admin_can_revoke_any(self, client, login_as, make_user):
        login_as(make_user())
        key_id = client.post("/api-keys", json={"name": "key"}).json()["id"]
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        assert client.delete(f"/api-keys/{key_id}").status_code == 204

    def test_missing_key_404(self, client, login_as, make_user):
        login_as(make_user())
        assert client.delete("/api-keys/99999").status_code == 404


class TestAdminManagement:
    def test_revoke_user_revokes_all_active_keys(self, client, login_as, make_user, db_session):
        user = make_user()
        login_as(user)
        first = client.post("/api-keys", json={"name": "first"}).json()["id"]
        client.post("/api-keys", json={"name": "second"})
        client.delete(f"/api-keys/{first}")  # already revoked — must not recount

        login_as(make_user(role=UserRole.SUPER_ADMIN))
        res = client.post(f"/api-keys/revoke-user/{user.id}")
        assert res.status_code == 200
        assert res.json() == {"revoked": 1}
        assert all(k.revoked_at is not None for k in db_session.query(ApiKey).all())

    @pytest.mark.parametrize("role", [UserRole.USER, UserRole.ADMIN])
    def test_revoke_user_requires_super_admin(self, client, login_as, make_user, role):
        target = make_user()
        login_as(make_user(role=role))
        assert client.post(f"/api-keys/revoke-user/{target.id}").status_code == 403

    def test_revoke_user_unknown_user_is_zero(self, client, login_as, make_user):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        assert client.post("/api-keys/revoke-user/99999").json() == {"revoked": 0}

    def test_revoked_by_records_who(self, client, login_as, make_user):
        user = make_user()
        login_as(user)
        own = client.post("/api-keys", json={"name": "self-revoked"}).json()["id"]
        other = client.post("/api-keys", json={"name": "admin-revoked"}).json()["id"]
        client.delete(f"/api-keys/{own}")

        admin = make_user(role=UserRole.SUPER_ADMIN)
        login_as(admin)
        client.delete(f"/api-keys/{other}")

        by_name = {k["name"]: k for k in client.get("/api-keys?all=true").json()}
        assert by_name["self-revoked"]["revoked_by_email"] == user.email
        assert by_name["admin-revoked"]["revoked_by_email"] == admin.email

    def test_search_filters_by_owner_email_and_key_name(self, client, login_as, make_user):
        alice = make_user(email="alice@example.com")
        bob = make_user(email="bob@example.com")
        login_as(alice)
        client.post("/api-keys", json={"name": "field survey"})
        login_as(bob)
        client.post("/api-keys", json={"name": "lab import"})

        login_as(make_user(role=UserRole.SUPER_ADMIN))
        by_email = client.get("/api-keys?all=true&search=alice").json()
        assert [k["owner_email"] for k in by_email] == ["alice@example.com"]
        by_name = client.get("/api-keys?all=true&search=lab").json()
        assert [k["name"] for k in by_name] == ["lab import"]

    def test_status_filter(self, client, login_as, make_user, db_session):
        user = make_user()
        login_as(user)
        client.post("/api-keys", json={"name": "active key"})
        revoked = client.post("/api-keys", json={"name": "revoked key"}).json()["id"]
        client.delete(f"/api-keys/{revoked}")
        expired = client.post("/api-keys", json={"name": "expired key"}).json()["id"]
        db_session.query(ApiKey).filter(ApiKey.id == expired).update(
            {ApiKey.expires_at: datetime.utcnow() - timedelta(days=1)}
        )
        db_session.commit()

        login_as(make_user(role=UserRole.SUPER_ADMIN))
        for status_value, expected in [
            ("active", "active key"), ("revoked", "revoked key"), ("expired", "expired key"),
        ]:
            names = [k["name"] for k in client.get(f"/api-keys?all=true&status={status_value}").json()]
            assert names == [expected], status_value

    def test_sort_by_last_used_nulls_last(self, client, login_as, make_user, db_session):
        login_as(make_user())
        used = client.post("/api-keys", json={"name": "used"}).json()["id"]
        client.post("/api-keys", json={"name": "never used"})
        db_session.query(ApiKey).filter(ApiKey.id == used).update(
            {ApiKey.last_used_at: datetime.utcnow()}
        )
        db_session.commit()

        login_as(make_user(role=UserRole.SUPER_ADMIN))
        names = [k["name"] for k in client.get("/api-keys?all=true&sort=last_used").json()]
        assert names == ["used", "never used"]
