"""HTTP-level tests for the /push router."""
import pytest

import app.notification.providers.webpush as webpush_module
from app.notification.models import PushSubscription

SUBSCRIPTION_KEYS = {
    "id", "endpoint", "provider", "browser", "platform", "device_name",
    "active", "last_used_at", "created_at",
}


def _subscription_body(endpoint="https://push.example.com/ep1", **overrides):
    body = {
        "endpoint": endpoint,
        "keys": {"p256dh": "client-public-key", "auth": "client-auth-secret"},
        "browser": "Chrome",
        "platform": "macOS",
        "device_name": "MacBook",
    }
    body.update(overrides)
    return body


@pytest.fixture
def user(make_user):
    return make_user()


@pytest.fixture
def auth(user, login_as):
    return login_as(user)


class TestAuthRequired:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/push/public-key"),
        ("POST", "/push/subscriptions"),
        ("GET", "/push/subscriptions"),
        ("DELETE", "/push/subscriptions"),
    ])
    def test_401_without_token(self, client, method, path):
        assert client.request(method, path, json={}).status_code == 401


class TestPublicKey:
    def test_null_when_vapid_unset(self, client, auth, monkeypatch):
        monkeypatch.setattr(webpush_module, "VAPID_PUBLIC_KEY", None)
        response = client.get("/push/public-key")
        assert response.status_code == 200
        assert response.json() == {"publicKey": None}

    def test_key_returned_when_configured(self, client, auth, monkeypatch):
        monkeypatch.setattr(webpush_module, "VAPID_PUBLIC_KEY", "test-vapid-key")
        assert client.get("/push/public-key").json() == {"publicKey": "test-vapid-key"}


class TestSubscribe:
    def test_create_subscription(self, client, auth, db_session):
        response = client.post("/push/subscriptions", json=_subscription_body())
        assert response.status_code == 201
        data = response.json()
        assert set(data) == SUBSCRIPTION_KEYS
        assert data["endpoint"] == "https://push.example.com/ep1"
        assert data["provider"] == "WEBPUSH"
        assert data["active"] is True
        row = db_session.query(PushSubscription).one()
        assert row.p256dh == "client-public-key"
        assert row.auth == "client-auth-secret"

    def test_validation_422(self, client, auth):
        assert client.post("/push/subscriptions", json={}).status_code == 422
        assert client.post(
            "/push/subscriptions",
            json={"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k"}},
        ).status_code == 422
        assert client.post(
            "/push/subscriptions", json=_subscription_body(endpoint="")
        ).status_code == 422

    def test_reregister_same_endpoint_updates_in_place(self, client, auth, db_session):
        first = client.post("/push/subscriptions", json=_subscription_body()).json()
        # Simulate accumulated failures + deactivation before re-registering.
        row = db_session.query(PushSubscription).one()
        row.failure_count = 4
        row.active = False
        db_session.commit()
        body = _subscription_body()
        body["keys"] = {"p256dh": "new-p256dh", "auth": "new-auth"}
        second = client.post("/push/subscriptions", json=body).json()
        assert second["id"] == first["id"]  # upsert, not a duplicate row
        assert second["active"] is True
        assert db_session.query(PushSubscription).count() == 1
        db_session.refresh(row)
        assert row.p256dh == "new-p256dh"
        assert row.failure_count == 0

    def test_same_endpoint_different_user_moves_ownership(self, client, app, db_session,
                                                          make_user, login_as):
        """Endpoint takeover: the endpoint identifies one browser profile, so
        re-registering under another account MOVES the row to the new user (by
        design per the repository docstring). The critical safety property is
        that the previous owner stops receiving pushes through it — asserted
        here via ownership of the row."""
        user_a = make_user()
        user_b = make_user()
        login_as(user_a)
        client.post("/push/subscriptions", json=_subscription_body())
        login_as(user_b)
        response = client.post("/push/subscriptions", json=_subscription_body())
        assert response.status_code == 201
        row = db_session.query(PushSubscription).one()  # still exactly one row
        assert row.user_id == user_b.id
        # user_a no longer lists (or receives through) the endpoint.
        login_as(user_a)
        assert client.get("/push/subscriptions").json() == []


class TestListSubscriptions:
    def test_lists_only_callers_rows(self, client, app, make_user, login_as):
        user_a = make_user()
        user_b = make_user()
        login_as(user_a)
        client.post("/push/subscriptions",
                    json=_subscription_body("https://push.example.com/a"))
        login_as(user_b)
        client.post("/push/subscriptions",
                    json=_subscription_body("https://push.example.com/b"))
        data = client.get("/push/subscriptions").json()
        assert [s["endpoint"] for s in data] == ["https://push.example.com/b"]

    def test_includes_inactive_rows(self, client, auth, db_session):
        client.post("/push/subscriptions", json=_subscription_body())
        row = db_session.query(PushSubscription).one()
        row.active = False
        db_session.commit()
        data = client.get("/push/subscriptions").json()
        assert len(data) == 1
        assert data[0]["active"] is False


class TestUnsubscribe:
    def test_delete_with_json_body(self, client, auth, db_session):
        client.post("/push/subscriptions", json=_subscription_body())
        response = client.request(
            "DELETE", "/push/subscriptions",
            json={"endpoint": "https://push.example.com/ep1"},
        )
        assert response.status_code == 204
        assert response.content == b""
        assert db_session.query(PushSubscription).count() == 0

    def test_idempotent(self, client, auth):
        for _ in range(2):
            response = client.request(
                "DELETE", "/push/subscriptions",
                json={"endpoint": "https://push.example.com/never-existed"},
            )
            assert response.status_code == 204

    def test_scoped_to_caller(self, client, app, db_session, make_user, login_as):
        owner = make_user()
        stranger = make_user()
        login_as(owner)
        client.post("/push/subscriptions", json=_subscription_body())
        login_as(stranger)
        response = client.request(
            "DELETE", "/push/subscriptions",
            json={"endpoint": "https://push.example.com/ep1"},
        )
        # Idempotent 204, but the OWNER's row must survive.
        assert response.status_code == 204
        row = db_session.query(PushSubscription).one()
        assert row.user_id == owner.id

    def test_validation_422(self, client, auth):
        assert client.request(
            "DELETE", "/push/subscriptions", json={"endpoint": ""}
        ).status_code == 422
