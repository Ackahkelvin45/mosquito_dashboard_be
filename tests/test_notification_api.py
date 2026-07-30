"""HTTP-level tests for the /notifications router."""
from datetime import datetime, timedelta

import pytest

from app.authentication.enums import UserRole
from app.notification.enums import (
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
)

PAGE_KEYS = {"items", "total", "page", "page_size", "total_pages"}
NOTIFICATION_KEYS = {
    "id", "title", "body", "notification_type", "severity", "category",
    "payload", "icon", "action_url", "cluster_id", "device_id",
    "read_at", "archived_at", "delivered_at", "created_at", "expires_at",
}


@pytest.fixture
def user(make_user):
    return make_user()


@pytest.fixture
def auth(user, login_as):
    return login_as(user)


class TestAuthRequired:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/notifications"),
        ("GET", "/notifications/unread-count"),
        ("PATCH", "/notifications/read-all"),
        ("GET", "/notifications/preferences"),
        ("PUT", "/notifications/preferences"),
        ("POST", "/notifications/test"),
        ("PATCH", "/notifications/1/read"),
        ("PATCH", "/notifications/1/unread"),
        ("PATCH", "/notifications/1/archive"),
        ("PATCH", "/notifications/1/unarchive"),
        ("DELETE", "/notifications/1"),
    ])
    def test_401_without_token(self, client, method, path):
        response = client.request(method, path, json={})
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}


class TestList:
    def test_empty_envelope(self, client, auth):
        response = client.get("/notifications")
        assert response.status_code == 200
        data = response.json()
        assert set(data) == PAGE_KEYS
        assert data == {"items": [], "total": 0, "page": 1,
                        "page_size": 20, "total_pages": 0}

    def test_item_shape(self, client, auth, user, make_notification):
        make_notification(user, payload={"a": 1})
        item = client.get("/notifications").json()["items"][0]
        assert set(item) == NOTIFICATION_KEYS
        assert "user_id" not in item

    def test_pagination_walk(self, client, auth, user, make_notification):
        ids = [make_notification(user).id for _ in range(25)]
        seen: list[int] = []
        for page in range(1, 4):
            data = client.get(f"/notifications?page={page}&page_size=10").json()
            assert data["total"] == 25
            assert data["total_pages"] == 3
            assert data["page"] == page
            seen.extend(item["id"] for item in data["items"])
        assert len(seen) == 25
        assert set(seen) == set(ids)
        # newest first by default
        assert seen == sorted(seen, reverse=True)

    def test_page_size_validation(self, client, auth):
        assert client.get("/notifications?page=0").status_code == 422
        assert client.get("/notifications?page_size=101").status_code == 422
        assert client.get("/notifications?sort=sideways").status_code == 422

    def test_filters_via_query_params(self, client, auth, user, make_notification):
        target = make_notification(
            user,
            title="Battery critical",
            body="replace now",
            notification_type=NotificationType.LOW_BATTERY,
            severity=NotificationSeverity.CRITICAL,
            category=NotificationCategory.SENSOR,
        )
        make_notification(user, read_at=datetime.utcnow())
        archived = make_notification(user, archived_at=datetime.utcnow())

        def ids(query):
            return [n["id"] for n in client.get(f"/notifications?{query}").json()["items"]]

        assert ids("category=SENSOR") == [target.id]
        assert ids("severity=CRITICAL") == [target.id]
        assert ids("type=LOW_BATTERY") == [target.id]
        assert ids("unread_only=true") == [target.id]
        assert ids("archived=true") == [archived.id]
        assert ids("search=battery") == [target.id]
        assert ids("search=replace") == [target.id]

    def test_invalid_enum_filter_is_422(self, client, auth):
        assert client.get("/notifications?severity=BOGUS").status_code == 422
        assert client.get("/notifications?category=BOGUS").status_code == 422
        assert client.get("/notifications?type=BOGUS").status_code == 422

    def test_sort_oldest(self, client, auth, user, make_notification, backdate):
        old = make_notification(user)
        backdate(old, minutes=90)
        new = make_notification(user)
        ids = [n["id"] for n in client.get("/notifications?sort=oldest").json()["items"]]
        assert ids == [old.id, new.id]


class TestUnreadCount:
    def test_count(self, client, auth, user, make_notification):
        make_notification(user)
        make_notification(user, read_at=datetime.utcnow())
        response = client.get("/notifications/unread-count")
        assert response.status_code == 200
        assert response.json() == {"count": 1}


class TestReadAll:
    def test_read_all_routing_and_count(self, client, auth, user, make_notification):
        # "read-all" must hit the static route, not /{id}/... parsing.
        for _ in range(3):
            make_notification(user)
        response = client.patch("/notifications/read-all")
        assert response.status_code == 200
        assert response.json() == {"updated": 3}
        assert client.get("/notifications/unread-count").json() == {"count": 0}
        assert client.patch("/notifications/read-all").json() == {"updated": 0}


class TestPerNotificationRoutes:
    def test_read_unread_cycle(self, client, auth, user, make_notification):
        n = make_notification(user)
        read = client.patch(f"/notifications/{n.id}/read")
        assert read.status_code == 200
        assert read.json()["read_at"] is not None
        unread = client.patch(f"/notifications/{n.id}/unread")
        assert unread.status_code == 200
        assert unread.json()["read_at"] is None

    def test_archive_unarchive_cycle(self, client, auth, user, make_notification):
        n = make_notification(user)
        archived = client.patch(f"/notifications/{n.id}/archive")
        assert archived.status_code == 200
        assert archived.json()["archived_at"] is not None
        unarchived = client.patch(f"/notifications/{n.id}/unarchive")
        assert unarchived.status_code == 200
        assert unarchived.json()["archived_at"] is None

    def test_delete_returns_204_then_404(self, client, auth, user, make_notification):
        n = make_notification(user)
        response = client.delete(f"/notifications/{n.id}")
        assert response.status_code == 204
        assert response.content == b""
        assert client.delete(f"/notifications/{n.id}").status_code == 404
        assert client.patch(f"/notifications/{n.id}/read").status_code == 404

    def test_unknown_id_404(self, client, auth):
        assert client.patch("/notifications/424242/read").status_code == 404
        assert client.delete("/notifications/424242").status_code == 404

    def test_non_int_id_422(self, client, auth):
        assert client.patch("/notifications/abc/read").status_code == 422


class TestPreferencesRoutes:
    def test_get_defaults(self, client, auth):
        response = client.get("/notifications/preferences")
        assert response.status_code == 200
        data = response.json()
        assert data == {
            "species_alerts": True,
            "battery_alerts": True,
            "offline_alerts": True,
            "admin_alerts": True,
            "researcher_alerts": True,
            "email_enabled": False,
            "push_enabled": True,
            "in_app_enabled": True,
        }

    def test_put_partial_update(self, client, auth):
        response = client.put(
            "/notifications/preferences",
            json={"species_alerts": False, "email_enabled": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["species_alerts"] is False
        assert data["email_enabled"] is True
        assert data["battery_alerts"] is True
        # Persisted.
        again = client.get("/notifications/preferences").json()
        assert again["species_alerts"] is False


class TestTestEndpoint:
    def test_requires_super_admin(self, client, make_user, login_as):
        for role in (UserRole.USER, UserRole.ADMIN):
            login_as(make_user(role=role))
            response = client.post("/notifications/test")
            assert response.status_code == 403, role
            assert response.json()["detail"] == (
                "You do not have permission to perform this action"
            )

    def test_super_admin_creates_test_notification(self, client, make_user, login_as):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        response = client.post("/notifications/test")
        assert response.status_code == 201
        data = response.json()
        assert data["notification_type"] == "TEST"
        assert set(data) == NOTIFICATION_KEYS

    def test_suppressed_test_is_400(self, client, make_user, login_as):
        admin = make_user(role=UserRole.SUPER_ADMIN)
        login_as(admin)
        client.put("/notifications/preferences", json={"in_app_enabled": False})
        response = client.post("/notifications/test")
        assert response.status_code == 400


class TestServiceErrorPropagation:
    """Service-raised HTTPExceptions surface unchanged through every route's
    try/except wrapper (both routers)."""

    @pytest.mark.parametrize("method,path,service_attr,body", [
        ("GET", "/notifications", "list_notifications", None),
        ("GET", "/notifications/unread-count", "unread_count", None),
        ("PATCH", "/notifications/read-all", "mark_all_read", None),
        ("GET", "/notifications/preferences", "get_preferences", None),
        ("PUT", "/notifications/preferences", "update_preferences", {}),
        ("GET", "/push/public-key", "get_vapid_public_key", None),
        ("POST", "/push/subscriptions", "subscribe_push", {
            "endpoint": "https://push.example.com/x",
            "keys": {"p256dh": "p", "auth": "a"},
        }),
        ("GET", "/push/subscriptions", "list_push_subscriptions", None),
        ("DELETE", "/push/subscriptions", "unsubscribe_push",
         {"endpoint": "https://push.example.com/x"}),
    ])
    def test_http_exception_passthrough(self, client, auth, monkeypatch,
                                        method, path, service_attr, body):
        from fastapi import HTTPException

        from app.notification.service import NotificationService

        def boom(self, *args, **kwargs):
            raise HTTPException(status_code=418, detail="teapot")

        monkeypatch.setattr(NotificationService, service_attr, boom)
        response = client.request(method, path, json=body)
        assert response.status_code == 418
        assert response.json() == {"detail": "teapot"}
