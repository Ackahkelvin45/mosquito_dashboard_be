"""Cross-user / cross-cluster isolation: notifications are strictly
per-recipient rows — no endpoint or filter combination may ever expose
another user's rows, whatever the caller's role."""
import itertools
from datetime import datetime

import pytest

from app.authentication.enums import UserRole
from app.notification.enums import (
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
)
from app.notification.models import Notification


@pytest.fixture
def two_cluster_world(make_cluster, make_user, make_notification):
    """User A in cluster 1, user B in cluster 2, one notification each,
    plus an admin and a super admin with rows of their own."""
    cluster1 = make_cluster()
    cluster2 = make_cluster()
    user_a = make_user(cluster_id=cluster1.id)
    user_b = make_user(cluster_id=cluster2.id)
    admin = make_user(role=UserRole.ADMIN, cluster_id=cluster1.id)
    super_admin = make_user(role=UserRole.SUPER_ADMIN)
    rows = {
        "a": make_notification(user_a, title="alpha row", cluster_id=cluster1.id),
        "b": make_notification(user_b, title="bravo row", cluster_id=cluster2.id),
        "admin": make_notification(admin, title="admin row", cluster_id=cluster1.id),
        "super": make_notification(super_admin, title="super row"),
    }
    return {
        "clusters": (cluster1, cluster2),
        "users": {"a": user_a, "b": user_b, "admin": admin, "super": super_admin},
        "rows": rows,
    }


def _all_filter_combos():
    """A representative sweep of every list filter dimension."""
    archived = ["", "archived=true", "archived=false"]
    unread = ["", "unread_only=true"]
    extra = [
        "", "category=SYSTEM", "severity=INFO", "type=TEST",
        "search=row", "sort=oldest", "page=1&page_size=100",
    ]
    for parts in itertools.product(archived, unread, extra):
        query = "&".join(p for p in parts if p)
        yield query


class TestListIsolation:
    @pytest.mark.parametrize("who", ["a", "b", "admin", "super"])
    def test_every_filter_combo_returns_only_own_rows(self, client, login_as,
                                                      two_cluster_world, who):
        world = two_cluster_world
        me = world["users"][who]
        my_row_id = world["rows"][who].id
        other_ids = {r.id for k, r in world["rows"].items() if k != who}
        login_as(me)
        for query in _all_filter_combos():
            data = client.get(f"/notifications?{query}").json()
            ids = {item["id"] for item in data["items"]}
            assert not (ids & other_ids), f"leak with query {query!r} as {who}"
            if "archived=true" not in query and "unread_only" not in query:
                assert ids == {my_row_id}, f"own row missing with query {query!r}"

    def test_admin_sees_only_own_rows_not_cluster_members(self, client, login_as,
                                                          two_cluster_world):
        # The admin shares cluster 1 with user A, but notifications are
        # per-recipient: the admin's list contains only the admin's row.
        world = two_cluster_world
        login_as(world["users"]["admin"])
        ids = {n["id"] for n in client.get("/notifications?page_size=100").json()["items"]}
        assert ids == {world["rows"]["admin"].id}

    def test_super_admin_sees_only_own_rows(self, client, login_as, two_cluster_world):
        world = two_cluster_world
        login_as(world["users"]["super"])
        ids = {n["id"] for n in client.get("/notifications?page_size=100").json()["items"]}
        assert ids == {world["rows"]["super"].id}

    def test_unread_count_is_per_user(self, client, login_as, two_cluster_world,
                                      make_notification):
        world = two_cluster_world
        make_notification(world["users"]["b"])  # second row for B
        login_as(world["users"]["a"])
        assert client.get("/notifications/unread-count").json() == {"count": 1}
        login_as(world["users"]["b"])
        assert client.get("/notifications/unread-count").json() == {"count": 2}

    def test_search_cannot_probe_other_users_titles(self, client, login_as,
                                                    two_cluster_world):
        world = two_cluster_world
        login_as(world["users"]["a"])
        data = client.get("/notifications?search=bravo").json()
        assert data["total"] == 0


class TestCrossUserActions:
    @pytest.mark.parametrize("who", ["a", "admin", "super"])
    def test_acting_on_someone_elses_row_is_404(self, client, login_as,
                                                two_cluster_world, who):
        # Whatever the caller's role, another user's notification id is a 404
        # (never a 403 — existence must not be revealed).
        world = two_cluster_world
        target_id = world["rows"]["b"].id
        login_as(world["users"][who])
        for method, path in [
            ("PATCH", f"/notifications/{target_id}/read"),
            ("PATCH", f"/notifications/{target_id}/unread"),
            ("PATCH", f"/notifications/{target_id}/archive"),
            ("PATCH", f"/notifications/{target_id}/unarchive"),
            ("DELETE", f"/notifications/{target_id}"),
        ]:
            response = client.request(method, path)
            assert response.status_code == 404, (who, method, path)

    def test_cross_user_action_leaves_row_untouched(self, client, login_as,
                                                    two_cluster_world, db_session):
        world = two_cluster_world
        target = world["rows"]["b"]
        login_as(world["users"]["a"])
        client.request("DELETE", f"/notifications/{target.id}")
        client.patch(f"/notifications/{target.id}/read")
        db_session.refresh(target)
        assert target.deleted_at is None
        assert target.read_at is None

    def test_read_all_scoped_to_caller(self, client, login_as, two_cluster_world,
                                       db_session):
        world = two_cluster_world
        login_as(world["users"]["a"])
        assert client.patch("/notifications/read-all").json() == {"updated": 1}
        db_session.refresh(world["rows"]["b"])
        assert world["rows"]["b"].read_at is None  # B's row stays unread

    def test_preferences_are_per_user(self, client, login_as, two_cluster_world):
        world = two_cluster_world
        login_as(world["users"]["a"])
        client.put("/notifications/preferences", json={"species_alerts": False})
        login_as(world["users"]["b"])
        assert client.get("/notifications/preferences").json()["species_alerts"] is True


class TestServiceLevelIsolation:
    def test_list_never_mixes_users_even_with_shared_metadata(
        self, service, make_user, make_cluster, make_notification
    ):
        # Same cluster_id, device_id-free rows, same dedupe key — the ONLY
        # separator is user_id.
        cluster = make_cluster()
        user_a = make_user(cluster_id=cluster.id)
        user_b = make_user(cluster_id=cluster.id)
        make_notification(user_a, cluster_id=cluster.id, dedupe_key="shared")
        make_notification(user_b, cluster_id=cluster.id, dedupe_key="shared")
        page_a = service.list_notifications(user_a.id, page_size=100)
        assert all(n.cluster_id == cluster.id for n in page_a.items)
        assert page_a.total == 1
