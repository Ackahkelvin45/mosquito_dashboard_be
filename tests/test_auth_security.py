"""FR-4 (email-OTP 2FA + session versioning), FR-27 (audit log), and the
security pre-fixes: login gates, OTP attempt caps, researcher-request RBAC,
refresh hardening."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from fastapi import FastAPI

import app.service.user_service as user_service_module
from app.audit.models import AuditAction, AuditLog
from app.authentication.enums import ApprovalStatus, UserRole
from app.authentication.models import LoginOTP
from app.authentication.repository.researcher_request_repository import ResearcherRequestRepository
from app.authentication.schema import ResearcherRequestCreate
from app.core.database import get_db

PASSWORD = "correct-horse-9"


@pytest.fixture
def app(db_session):
    """Auth + audit routers so the real login/2FA/refresh paths run."""
    from app.audit.routes import router as audit_router
    from app.authentication.routes import router as auth_router

    application = FastAPI()
    application.include_router(auth_router, prefix="/auth")
    application.include_router(audit_router, prefix="/audit-logs")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture(autouse=True)
def reset_rate_limits():
    from app.api_access import auth as api_auth
    api_auth._windows.clear()
    yield
    api_auth._windows.clear()


@pytest.fixture(autouse=True)
def sent_codes(monkeypatch):
    """Capture 2FA codes instead of emailing them."""
    codes: list[tuple[str, str]] = []  # (email, code)
    monkeypatch.setattr(user_service_module, "send_two_factor_code_email",
                        lambda to, first_name, code: codes.append((to, code)))
    return codes


def _actions(db_session):
    return [row.action for row in db_session.query(AuditLog).all()]


def _login(client, email, password=PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


class TestLoginGates:
    def test_plain_user_gets_tokens_and_audit(self, client, db_session, make_user):
        user = make_user(password=PASSWORD)
        res = _login(client, user.email)
        assert res.status_code == 200
        body = res.json()
        assert body["access_token"] and body["refresh_token"]
        assert AuditAction.LOGIN_SUCCESS in _actions(db_session)

    def test_unknown_email_and_wrong_password_are_identical_400s(self, client,
                                                                 db_session, make_user):
        user = make_user(password=PASSWORD)
        res1 = _login(client, "ghost@example.com")
        res2 = _login(client, user.email, "wrong-password-1")
        assert res1.status_code == res2.status_code == 400
        assert res1.json() == res2.json()
        assert _actions(db_session).count(AuditAction.LOGIN_FAILED) == 2

    def test_inactive_or_unapproved_rejected_despite_password(self, client,
                                                              db_session, make_user):
        inactive = make_user(password=PASSWORD, is_active=False)
        pending = make_user(password=PASSWORD,
                            approval_status=ApprovalStatus.PENDING)
        assert _login(client, inactive.email).status_code == 403
        assert _login(client, pending.email).status_code == 403


class TestTwoFactor:
    def test_admin_login_returns_challenge_not_tokens(self, client, db_session,
                                                      make_user, sent_codes):
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD, two_factor_enabled=True)
        res = _login(client, admin.email)
        assert res.status_code == 200
        body = res.json()
        assert body["two_factor_required"] is True
        assert "access_token" not in body
        assert len(sent_codes) == 1 and sent_codes[0][0] == admin.email
        assert db_session.query(LoginOTP).count() == 1
        assert AuditAction.TWO_FACTOR_SENT in _actions(db_session)

    def test_correct_code_yields_tokens(self, client, db_session, make_user,
                                        sent_codes):
        admin = make_user(role=UserRole.SUPER_ADMIN, password=PASSWORD, two_factor_enabled=True)
        challenge = _login(client, admin.email).json()["two_factor_token"]
        code = sent_codes[0][1]
        res = client.post("/auth/login/verify-2fa",
                          json={"two_factor_token": challenge, "code": code})
        assert res.status_code == 200
        assert res.json()["access_token"]
        assert AuditAction.TWO_FACTOR_SUCCESS in _actions(db_session)
        # Single-use: replaying the same challenge+code fails.
        replay = client.post("/auth/login/verify-2fa",
                             json={"two_factor_token": challenge, "code": code})
        assert replay.status_code == 400

    def test_wrong_code_burns_after_cap(self, client, db_session, make_user,
                                        sent_codes):
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD, two_factor_enabled=True)
        challenge = _login(client, admin.email).json()["two_factor_token"]
        for _ in range(5):
            res = client.post("/auth/login/verify-2fa",
                              json={"two_factor_token": challenge, "code": "000000"})
            assert res.status_code == 400
        # Burned: even the REAL code no longer works.
        real = sent_codes[0][1]
        res = client.post("/auth/login/verify-2fa",
                          json={"two_factor_token": challenge, "code": real})
        assert res.status_code == 400

    def test_expired_code_rejected(self, client, db_session, make_user, sent_codes):
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD, two_factor_enabled=True)
        challenge = _login(client, admin.email).json()["two_factor_token"]
        db_session.query(LoginOTP).update(
            {"expires_at": datetime.now() - timedelta(minutes=1)})
        db_session.commit()
        res = client.post("/auth/login/verify-2fa",
                          json={"two_factor_token": challenge, "code": sent_codes[0][1]})
        assert res.status_code == 400

    def test_resend_cooldown_then_new_code(self, client, db_session, make_user,
                                           sent_codes):
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD, two_factor_enabled=True)
        challenge = _login(client, admin.email).json()["two_factor_token"]
        old_code = sent_codes[0][1]
        # Inside the cooldown: refused.
        assert client.post("/auth/login/resend-2fa",
                           json={"two_factor_token": challenge}).status_code == 429
        db_session.query(LoginOTP).update(
            {"last_sent_at": datetime.now() - timedelta(minutes=2)})
        db_session.commit()
        assert client.post("/auth/login/resend-2fa",
                           json={"two_factor_token": challenge}).status_code == 200
        new_code = sent_codes[-1][1]
        # The old code is dead; the new one works.
        if old_code != new_code:  # 1-in-a-million collision guard
            assert client.post("/auth/login/verify-2fa",
                               json={"two_factor_token": challenge, "code": old_code}
                               ).status_code == 400
        assert client.post("/auth/login/verify-2fa",
                           json={"two_factor_token": challenge, "code": new_code}
                           ).status_code == 200

    def test_plain_user_opt_in_flow(self, client, db_session, make_user, sent_codes):
        user = make_user(password=PASSWORD)
        # No 2FA by default.
        assert "access_token" in _login(client, user.email).json()
        # Enable via the real token (self-service endpoint requires auth).
        token = _login(client, user.email).json()["access_token"]
        res = client.post("/auth/me/two-factor",
                          json={"enabled": True, "current_password": PASSWORD},
                          headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        assert res.json() == {"two_factor_enabled": True, "reauth_required": True}
        # Enabling bumped token_version — the old token is dead...
        assert client.post("/auth/me/two-factor",
                           json={"enabled": False, "current_password": PASSWORD},
                           headers={"Authorization": f"Bearer {token}"}).status_code == 401
        # ...and the next login challenges.
        assert _login(client, user.email).json()["two_factor_required"] is True

    def test_admin_can_now_disable(self, client, db_session, make_user, sent_codes):
        """2FA is per-user: an admin may turn it off in Settings (the migration
        seeds admins ON, but the role no longer forces it)."""
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD, two_factor_enabled=True)
        challenge = _login(client, admin.email).json()["two_factor_token"]
        token = client.post("/auth/login/verify-2fa",
                            json={"two_factor_token": challenge,
                                  "code": sent_codes[0][1]}).json()["access_token"]
        res = client.post("/auth/me/two-factor",
                          json={"enabled": False, "current_password": PASSWORD},
                          headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        assert res.json()["two_factor_enabled"] is False
        # Next login: straight to tokens, no challenge.
        assert "access_token" in _login(client, admin.email).json()

    def test_admin_without_flag_logs_in_directly(self, client, db_session, make_user):
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD)
        body = _login(client, admin.email).json()
        assert "access_token" in body and "two_factor_required" not in body

class TestSessionVersioning:
    def test_refresh_in_body_and_rotated_out_on_deactivation(self, client,
                                                             db_session, make_user):
        user = make_user(password=PASSWORD)
        tokens = _login(client, user.email).json()
        res = client.post("/auth/refresh-token",
                          json={"refresh_token": tokens["refresh_token"]})
        assert res.status_code == 200
        # Deactivation kills refresh immediately.
        user.is_active = False
        db_session.commit()
        res = client.post("/auth/refresh-token",
                          json={"refresh_token": tokens["refresh_token"]})
        assert res.status_code == 401

    def test_password_reset_invalidates_existing_tokens(self, client, db_session,
                                                        make_user):
        user = make_user(password=PASSWORD)
        tokens = _login(client, user.email).json()
        user.token_version = (user.token_version or 0) + 1  # what reset does
        db_session.commit()
        assert client.post("/auth/refresh-token",
                           json={"refresh_token": tokens["refresh_token"]}).status_code == 401
        assert client.get("/auth/me",
                          headers={"Authorization": f"Bearer {tokens['access_token']}"}
                          ).status_code == 401


class TestResetOtpHardening:
    def test_reset_otp_burns_after_cap(self, client, db_session, make_user,
                                       monkeypatch):
        import app.authentication.routes as auth_routes
        monkeypatch.setattr(auth_routes, "send_password_reset_otp_email",
                            lambda *a, **k: None)
        user = make_user(password=PASSWORD)
        captured = {}
        original = user_service_module.UserService.forgot_password

        def capture(self, email):
            result = original(self, email)
            captured.update(result or {})
            return result

        monkeypatch.setattr(user_service_module.UserService, "forgot_password", capture)
        assert client.post("/auth/forgot-password",
                           json={"email": user.email}).status_code == 200
        for _ in range(5):
            assert client.post("/auth/verify-otp",
                               json={"email": user.email, "otp": "000000"}).status_code == 400
        # Burned: the real code is now rejected too.
        assert client.post("/auth/verify-otp",
                           json={"email": user.email, "otp": captured["otp"]}).status_code == 400


class TestResearcherRbac:
    def test_requests_endpoints_super_admin_only(self, client, login_as, make_user):
        for role in (UserRole.USER, UserRole.ADMIN):
            login_as(make_user(role=role))
            assert client.get("/auth/researcher-requests").status_code == 403
            assert client.patch("/auth/researcher-requests/1/status?status=approved"
                                ).status_code == 403
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        assert client.get("/auth/researcher-requests").status_code == 200


class TestListFilters:
    """Query params behind the Users / Researcher Requests search + status UI."""

    def test_users_search_role_status_filters(self, client, login_as, make_user):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        ama = make_user(first_name="Ama", last_name="Mensah", email="ama.filters@example.com")
        kofi = make_user(role=UserRole.ADMIN, first_name="Kofi", last_name="Boateng",
                         email="kb.filters@example.com", approval_status=ApprovalStatus.PENDING)

        def ids(qs):
            res = client.get(f"/auth/users?page_size=100&{qs}")
            assert res.status_code == 200
            return {u["id"] for u in res.json()["items"]}

        assert ids("search=mensah") == {ama.id}            # last name, case-insensitive
        assert ids("search=KB.FILTERS@") == {kofi.id}      # email substring
        assert kofi.id in ids("role=ADMIN") and ama.id not in ids("role=ADMIN")
        assert kofi.id in ids("approval_status=PENDING") and ama.id not in ids("approval_status=PENDING")
        assert ids("search=filters&role=USER") == {ama.id}

    def test_users_search_case_insensitive_under_case_sensitive_like(self, client, login_as, db_session, make_user):
        # SQLite's LIKE ignores ASCII case, which would hide ilike -> like.
        if db_session.bind.dialect.name != "sqlite":
            pytest.skip("Postgres LIKE is already case-sensitive")
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        ama = make_user(last_name="Mensah")
        db_session.execute(text("PRAGMA case_sensitive_like = ON"))
        try:
            items = client.get("/auth/users?search=mensah").json()["items"]
        finally:
            db_session.execute(text("PRAGMA case_sensitive_like = OFF"))
        assert [u["id"] for u in items] == [ama.id]

    def test_users_filters_keep_cluster_admin_scope(self, client, login_as, make_user, make_cluster):
        mine, other = make_cluster(), make_cluster()
        member = make_user(cluster_id=mine.id, last_name="Scoped")
        make_user(cluster_id=other.id, last_name="Scoped")
        make_user(last_name="Scoped")  # cluster-less
        login_as(make_user(role=UserRole.ADMIN, cluster_id=mine.id))

        body = client.get("/auth/users?search=scoped&role=USER").json()
        assert [u["id"] for u in body["items"]] == [member.id]
        assert body["total"] == 1

    def _request(self, db_session, user, status=None):
        repo = ResearcherRequestRepository(db_session)
        request = repo.create_researcher_request(ResearcherRequestCreate(user_id=user.id))
        if status:
            repo.update_researcher_request_status(request.id, status)
        return request

    def test_researcher_requests_status_and_search(self, client, login_as, db_session, make_user):
        pending = self._request(db_session, make_user(first_name="Esi", email="esi.rr@example.com"))
        rejected = self._request(db_session, make_user(first_name="Yaw", email="yaw.rr@example.com"),
                                 status="rejected")

        for role in (UserRole.USER, UserRole.ADMIN):
            login_as(make_user(role=role))
            assert client.get("/auth/researcher-requests?status=pending").status_code == 403
            assert client.get("/auth/researcher-requests?search=esi").status_code == 403

        login_as(make_user(role=UserRole.SUPER_ADMIN))

        def ids(qs):
            res = client.get(f"/auth/researcher-requests?page_size=100&{qs}")
            assert res.status_code == 200
            return {r["id"] for r in res.json()["items"]}

        assert ids("") == {pending.id, rejected.id}
        assert ids("status=pending") == {pending.id}
        assert ids("status=rejected") == {rejected.id}
        assert ids("status=approved") == set()
        assert ids("search=ESI") == {pending.id}
        assert ids("search=yaw.rr@") == {rejected.id}
        assert ids("search=esi&status=rejected") == set()
        assert client.get("/auth/researcher-requests?status=bogus").status_code == 422


class TestResearcherApprovalMembership:
    """Approval must set users.cluster_id (what alerts/scoping read), but
    never move a user who already belongs to another cluster."""

    def _approve(self, db_session, user, cluster):
        repo = ResearcherRequestRepository(db_session)
        request = repo.create_researcher_request(
            ResearcherRequestCreate(user_id=user.id, cluster_id=cluster.id))
        repo.update_researcher_request_status(request.id, "approved")
        db_session.refresh(user)
        db_session.refresh(cluster)

    def test_sets_cluster_id_when_null(self, db_session, make_user, make_cluster):
        user, cluster = make_user(), make_cluster()
        self._approve(db_session, user, cluster)
        assert user.cluster_id == cluster.id
        assert user in cluster.cluster_admins

    def test_leaves_existing_cluster_id_alone(self, db_session, make_user,
                                              make_cluster):
        home, requested = make_cluster(), make_cluster()
        user = make_user(cluster_id=home.id)
        self._approve(db_session, user, requested)
        assert user.cluster_id == home.id
        assert user in requested.cluster_admins

    def test_patch_approve_with_new_cluster_uses_that_cluster(
            self, db_session, make_user, make_cluster):
        from app.authentication.schema import UpdateResearcherRequest
        original, chosen = make_cluster(), make_cluster()
        user = make_user()
        repo = ResearcherRequestRepository(db_session)
        request = repo.create_researcher_request(
            ResearcherRequestCreate(user_id=user.id, cluster_id=original.id))
        repo.update_reseacher_request(
            request.id, UpdateResearcherRequest(status="approved", cluster_id=chosen.id))
        db_session.refresh(user)
        db_session.refresh(chosen)
        assert user.cluster_id == chosen.id
        assert user in chosen.cluster_admins


class TestResearcherRejectionRevokesMembership:
    """Rejecting an APPROVED request clears users.cluster_id only when it is
    still the request's cluster; rejecting a pending one changes nothing."""

    @pytest.fixture(autouse=True)
    def _no_emails(self, monkeypatch):
        import app.authentication.routes as auth_routes
        for name in ("send_researcher_approved_email", "send_researcher_declined_email"):
            monkeypatch.setattr(auth_routes, name, lambda *a, **k: None)

    def _request(self, db_session, user, cluster, approve):
        repo = ResearcherRequestRepository(db_session)
        request = repo.create_researcher_request(
            ResearcherRequestCreate(user_id=user.id, cluster_id=cluster.id))
        if approve:
            repo.update_researcher_request_status(request.id, "approved")
        return request

    def _reject(self, client, request_id, route):
        if route == "status":
            return client.patch(f"/auth/researcher-requests/{request_id}/status?status=rejected")
        return client.patch(f"/auth/researcher-requests/{request_id}", json={"status": "rejected"})

    def _decisions(self, db_session):
        return [row.detail for row in db_session.query(AuditLog).filter(
            AuditLog.action == AuditAction.RESEARCHER_REQUEST_DECIDED).all()]

    @pytest.mark.parametrize("route", ["status", "patch"])
    def test_rejecting_approved_clears_cluster(self, client, login_as, db_session,
                                               make_user, make_cluster, route):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        user, cluster = make_user(), make_cluster()
        request = self._request(db_session, user, cluster, approve=True)
        db_session.refresh(user)
        assert user.cluster_id == cluster.id

        assert self._reject(client, request.id, route).status_code == 200
        db_session.refresh(user)
        assert user.cluster_id is None
        assert {"status": "rejected", "revoked_cluster_id": cluster.id} in self._decisions(db_session)

    @pytest.mark.parametrize("route", ["status", "patch"])
    def test_rejecting_approved_keeps_other_cluster(self, client, login_as, db_session,
                                                    make_user, make_cluster, route):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        home, requested = make_cluster(), make_cluster()
        user = make_user(cluster_id=home.id)
        request = self._request(db_session, user, requested, approve=True)

        assert self._reject(client, request.id, route).status_code == 200
        db_session.refresh(user)
        assert user.cluster_id == home.id
        assert not any("revoked_cluster_id" in (d or {}) for d in self._decisions(db_session))

    @pytest.mark.parametrize("route", ["status", "patch"])
    def test_rejecting_pending_leaves_cluster(self, client, login_as, db_session,
                                              make_user, make_cluster, route):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        cluster = make_cluster()
        user = make_user(cluster_id=cluster.id)
        request = self._request(db_session, user, cluster, approve=False)

        assert self._reject(client, request.id, route).status_code == 200
        db_session.refresh(user)
        assert user.cluster_id == cluster.id
        # Both routes audit every decision; no revocation in the detail.
        assert self._decisions(db_session) == [{"status": "rejected"}]

    @pytest.mark.parametrize("route", ["status", "patch"])
    def test_reject_drops_cluster_admin_and_reapprove_restores(
            self, client, login_as, db_session, make_user, make_cluster, route):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        user, cluster = make_user(), make_cluster()
        request = self._request(db_session, user, cluster, approve=True)
        db_session.refresh(cluster)
        assert user in cluster.cluster_admins

        assert self._reject(client, request.id, route).status_code == 200
        db_session.refresh(user)
        db_session.refresh(cluster)
        assert user.cluster_id is None
        assert user not in cluster.cluster_admins

        assert client.patch(f"/auth/researcher-requests/{request.id}/status?status=approved"
                            ).status_code == 200
        db_session.refresh(user)
        db_session.refresh(cluster)
        assert user.cluster_id == cluster.id
        assert user in cluster.cluster_admins

    def test_patch_cluster_only_on_approved_request_is_409(
            self, client, login_as, db_session, make_user, make_cluster):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        approved, other = make_cluster(), make_cluster()
        user = make_user()
        request = self._request(db_session, user, approved, approve=True)

        res = client.patch(f"/auth/researcher-requests/{request.id}", json={"cluster_id": other.id})
        assert res.status_code == 409
        assert "set status" in res.json()["detail"]
        db_session.refresh(request)
        assert request.cluster_id == approved.id

    def test_patch_cluster_only_on_pending_request_still_allowed(
            self, client, login_as, db_session, make_user, make_cluster):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        first, other = make_cluster(), make_cluster()
        request = self._request(db_session, make_user(), first, approve=False)

        assert client.patch(f"/auth/researcher-requests/{request.id}",
                            json={"cluster_id": other.id}).status_code == 200
        db_session.refresh(request)
        assert request.cluster_id == other.id
        assert self._decisions(db_session) == []  # not a decision, not audited

    def test_patch_approve_is_audited(self, client, login_as, db_session,
                                      make_user, make_cluster):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        request = self._request(db_session, make_user(), make_cluster(), approve=False)
        assert client.patch(f"/auth/researcher-requests/{request.id}",
                            json={"status": "approved"}).status_code == 200
        assert self._decisions(db_session) == [{"status": "approved"}]

    def _set(self, client, request_id, status):
        return client.patch(f"/auth/researcher-requests/{request_id}/status?status={status}")

    def test_approved_to_pending_revokes_then_reject_does_nothing_more(
            self, client, login_as, db_session, make_user, make_cluster):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        user, cluster = make_user(), make_cluster()
        request = self._request(db_session, user, cluster, approve=True)

        assert self._set(client, request.id, "pending").status_code == 200
        db_session.refresh(user)
        db_session.refresh(cluster)
        assert user.cluster_id is None
        assert user not in cluster.cluster_admins
        assert self._decisions(db_session) == [{"status": "pending", "revoked_cluster_id": cluster.id}]

        assert self._set(client, request.id, "rejected").status_code == 200
        db_session.refresh(user)
        assert user.cluster_id is None
        assert self._decisions(db_session)[-1] == {"status": "rejected"}

    def test_approved_to_pending_to_approved_restores(
            self, client, login_as, db_session, make_user, make_cluster):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        user, cluster = make_user(), make_cluster()
        request = self._request(db_session, user, cluster, approve=True)

        assert self._set(client, request.id, "pending").status_code == 200
        assert self._set(client, request.id, "approved").status_code == 200
        db_session.refresh(user)
        db_session.refresh(cluster)
        assert user.cluster_id == cluster.id
        assert user in cluster.cluster_admins

    def test_patch_approve_with_new_cluster_on_approved_is_409(
            self, client, login_as, db_session, make_user, make_cluster):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        a, b = make_cluster(), make_cluster()
        user = make_user()
        request = self._request(db_session, user, a, approve=True)

        res = client.patch(f"/auth/researcher-requests/{request.id}",
                           json={"status": "approved", "cluster_id": b.id})
        assert res.status_code == 409
        db_session.refresh(request)
        db_session.refresh(user)
        db_session.refresh(b)
        assert request.cluster_id == a.id
        assert user.cluster_id == a.id
        assert user not in b.cluster_admins

    def test_patch_reject_with_new_cluster_revokes_old_cluster(
            self, client, login_as, db_session, make_user, make_cluster):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        a, b = make_cluster(), make_cluster()
        user = make_user()
        request = self._request(db_session, user, a, approve=True)

        assert client.patch(f"/auth/researcher-requests/{request.id}",
                            json={"status": "rejected", "cluster_id": b.id}).status_code == 200
        db_session.refresh(user)
        db_session.refresh(a)
        assert user.cluster_id is None
        assert user not in a.cluster_admins
        assert self._decisions(db_session) == [{"status": "rejected", "revoked_cluster_id": a.id}]

    def test_reject_when_user_in_other_cluster_drops_stale_admin_entry(
            self, client, login_as, db_session, make_user, make_cluster):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        home, requested = make_cluster(), make_cluster()
        user = make_user(cluster_id=home.id)
        request = self._request(db_session, user, requested, approve=True)
        db_session.refresh(requested)
        assert user in requested.cluster_admins

        assert self._reject(client, request.id, "status").status_code == 200
        db_session.refresh(user)
        db_session.refresh(requested)
        assert user.cluster_id == home.id
        assert user not in requested.cluster_admins

    def test_patch_reject_with_new_cluster_revokes_approved_one(
            self, client, login_as, db_session, make_user, make_cluster):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        approved, other = make_cluster(), make_cluster()
        user = make_user()
        request = self._request(db_session, user, approved, approve=True)

        assert client.patch(f"/auth/researcher-requests/{request.id}",
                            json={"status": "rejected", "cluster_id": other.id}).status_code == 200
        db_session.refresh(user)
        assert user.cluster_id is None

    @pytest.mark.parametrize("route", ["status", "patch"])
    def test_rejection_removes_private_cluster_data_end_to_end(
            self, db_session, make_user, make_cluster, make_device, route):
        """Real bearer tokens (no auth override): after rejection the
        researcher's same token no longer reaches the private cluster's
        devices or readings; public data stays visible."""
        from fastapi.testclient import TestClient
        from app.core.security.authhandler import AuthHandler
        from app.authentication.routes import router as auth_router
        from app.device.models import MosquitoEvent, SensorDeviceReading
        from app.device.routes import router as device_router
        from app.mosquito.routes import router as mosquito_router

        application = FastAPI()
        application.include_router(auth_router, prefix="/auth")
        application.include_router(device_router, prefix="/devices")
        application.include_router(mosquito_router, prefix="/mosquito")
        application.dependency_overrides[get_db] = lambda: db_session
        client = TestClient(application, raise_server_exceptions=False)

        def bearer(u):
            return {"Authorization": f"Bearer {AuthHandler.create_access_token(u.id, u.token_version or 0)}"}

        admin = make_user(role=UserRole.SUPER_ADMIN)
        user = make_user()
        private, public = make_cluster(public=False), make_cluster(public=True)
        dpriv, dpub = make_device(private), make_device(public)
        now = datetime.utcnow()
        for d in (dpriv, dpub):
            db_session.add(MosquitoEvent(device_id=d.id, timestamp=now, count=1))
            db_session.add(SensorDeviceReading(device_id=d.id, timestamp=now, battery_voltage=3.9))
        db_session.commit()

        request = self._request(db_session, user, private, approve=True)
        hdr = bearer(user)

        def seen():
            devs = {d["device_uuid"] for d in client.get("/devices?page_size=100", headers=hdr).json()["items"]}
            sensor_devs = {r["device_id"] for r in client.get("/devices/sensor-readings?page_size=100",
                                                              headers=hdr).json()["items"]}
            mosq_devs = {r["device_id"] for r in client.get("/mosquito?page_size=100", headers=hdr).json()["items"]}
            clusters = {c["id"] for c in client.get("/devices/clusters?page_size=100", headers=hdr).json()["items"]}
            return {
                "devices": devs, "sensor": sensor_devs, "mosquito": mosq_devs, "clusters": clusters,
                "by_uuid": client.get(f"/devices/uuid/{dpriv.device_uuid}", headers=hdr).status_code,
                "by_id": client.get(f"/devices/{dpriv.id}", headers=hdr).status_code,
                "dev_sensor": client.get(f"/devices/uuid/{dpriv.device_uuid}/sensor-readings",
                                         headers=hdr).status_code,
                "dev_mosq": client.get(f"/devices/uuid/{dpriv.device_uuid}/mosquito-events",
                                       headers=hdr).status_code,
                "cluster": client.get(f"/devices/clusters/{private.id}", headers=hdr).status_code,
                "filtered": client.get(f"/mosquito?cluster_id={private.id}", headers=hdr).json()["items"],
            }

        before = seen()
        assert before["devices"] == {dpriv.device_uuid, dpub.device_uuid}
        assert before["sensor"] == before["mosquito"] == {dpriv.id, dpub.id}
        assert before["clusters"] == {private.id, public.id}
        assert before["by_uuid"] == before["by_id"] == before["dev_sensor"] == before["dev_mosq"] \
            == before["cluster"] == 200
        assert before["filtered"]

        if route == "status":
            res = client.patch(f"/auth/researcher-requests/{request.id}/status?status=rejected",
                               headers=bearer(admin))
        else:
            res = client.patch(f"/auth/researcher-requests/{request.id}",
                               json={"status": "rejected"}, headers=bearer(admin))
        assert res.status_code == 200

        after = seen()  # same researcher token, no re-login
        assert after["devices"] == {dpub.device_uuid}
        assert after["sensor"] == after["mosquito"] == {dpub.id}
        assert after["clusters"] == {public.id}
        assert after["by_uuid"] == after["by_id"] == after["dev_sensor"] == after["dev_mosq"] \
            == after["cluster"] == 404
        assert after["filtered"] == []


class TestClusterlessAdminManagesNobody:
    """A non-super-admin ADMIN with users.cluster_id = None (e.g. a researcher
    made ADMIN, then rejected) must not be treated as unrestricted."""

    @pytest.fixture(autouse=True)
    def _no_emails(self, monkeypatch):
        import app.authentication.routes as auth_routes
        monkeypatch.setattr(auth_routes, "send_welcome_email", lambda *a, **k: None)

    def _new_user(self):
        return {"email": f"new-{datetime.utcnow().timestamp()}@example.com",
                "password": "Password1", "first_name": "Aa", "last_name": "Bb"}

    def test_clusterless_admin_sees_and_manages_nobody(self, client, login_as, make_user):
        loose = make_user()  # cluster-less target
        login_as(make_user(role=UserRole.ADMIN))

        body = client.get("/auth/users").json()
        assert body["items"] == [] and body["total"] == 0
        assert client.get(f"/auth/users/{loose.id}").status_code == 404
        assert client.patch(f"/auth/users/{loose.id}",
                            json={"role": "ADMIN"}).status_code == 404
        assert client.post("/auth/register", json=self._new_user()).status_code == 403

    def test_cluster_admin_still_works(self, client, login_as, make_user, make_cluster):
        cluster = make_cluster()
        member, outsider, loose = make_user(cluster_id=cluster.id), make_user(cluster_id=make_cluster().id), make_user()
        admin = make_user(role=UserRole.ADMIN, cluster_id=cluster.id)
        login_as(admin)

        ids = {u["id"] for u in client.get("/auth/users?page_size=100").json()["items"]}
        assert ids == {member.id, admin.id}
        assert client.get(f"/auth/users/{member.id}").status_code == 200
        assert client.patch(f"/auth/users/{member.id}",
                            json={"first_name": "Renamed"}).status_code == 200
        for other in (outsider, loose):
            assert client.get(f"/auth/users/{other.id}").status_code == 404
        res = client.post("/auth/register", json=self._new_user())
        assert res.status_code == 201
        assert res.json()["cluster_id"] == cluster.id


class TestAuditEndpoint:
    def test_super_admin_only_and_filters(self, client, login_as, make_user,
                                          db_session):
        from app.audit.recorder import audit
        audit(db_session, AuditAction.DEVICE_CREATED, actor_email="a@x.com",
              target_type="device", target_id=1)
        audit(db_session, AuditAction.LOGIN_FAILED, actor_email="b@x.com")

        login_as(make_user(role=UserRole.ADMIN))
        assert client.get("/audit-logs").status_code == 403

        login_as(make_user(role=UserRole.SUPER_ADMIN))
        body = client.get("/audit-logs").json()
        assert body["total"] == 2
        only = client.get("/audit-logs?action=LOGIN_FAILED").json()
        assert only["total"] == 1
        assert only["items"][0]["actor_email"] == "b@x.com"
        assert set(client.get("/audit-logs/actions").json()) >= {
            "LOGIN_FAILED", "DEVICE_CREATED", "ALERT_SETTINGS_UPDATED"}


class TestSelfServiceProfile:
    """PATCH /auth/me (names only) and POST /auth/me/change-password."""

    def _token(self, client, email):
        return _login(client, email).json()["access_token"]

    def test_update_me_changes_names_and_audits(self, client, db_session, make_user):
        user = make_user(password=PASSWORD)
        token = self._token(client, user.email)
        res = client.patch("/auth/me",
                           json={"first_name": "Kwame", "last_name": "Mensah"},
                           headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        body = res.json()
        assert body["first_name"] == "Kwame" and body["last_name"] == "Mensah"
        assert AuditAction.PROFILE_UPDATED in _actions(db_session)
        # Email/role are not part of the contract — extra keys are ignored by
        # the schema, never applied.
        res = client.patch("/auth/me",
                           json={"email": "evil@x.com", "role": "SUPER_ADMIN"},
                           headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        assert res.json()["email"] == user.email
        assert res.json()["role"] == "USER"

    def test_change_password_requires_current_and_kills_sessions(self, client,
                                                                 db_session, make_user):
        user = make_user(password=PASSWORD)
        tokens = _login(client, user.email).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        # Wrong current password → rejected.
        res = client.post("/auth/me/change-password",
                          json={"current_password": "wrong-pass-123A",
                                "new_password": "NewPass123"},
                          headers=headers)
        assert res.status_code == 400

        # Policy applies to the new password.
        res = client.post("/auth/me/change-password",
                          json={"current_password": PASSWORD,
                                "new_password": "alllowercase1"},
                          headers=headers)
        assert res.status_code == 422

        # Success: reauth flagged, old tokens dead, new password logs in.
        res = client.post("/auth/me/change-password",
                          json={"current_password": PASSWORD,
                                "new_password": "NewPass123"},
                          headers=headers)
        assert res.status_code == 200
        assert res.json()["reauth_required"] is True
        assert AuditAction.PASSWORD_CHANGED in _actions(db_session)
        assert client.get("/auth/me", headers=headers).status_code == 401
        assert client.post("/auth/refresh-token",
                           json={"refresh_token": tokens["refresh_token"]}).status_code == 401
        assert _login(client, user.email, "NewPass123").status_code == 200
        assert _login(client, user.email, PASSWORD).status_code == 400


class TestGlobalTwoFactorSwitch:
    """The super-admin master gate: off = nobody is challenged at login,
    per-user flags stay stored and wake up when it's turned back on."""

    def _put(self, client, enabled):
        return client.put("/auth/security-settings",
                          json={"two_factor_login_enabled": enabled})

    def test_default_is_enabled(self, client, login_as, make_user):
        login_as(make_user())
        body = client.get("/auth/security-settings").json()
        assert body == {"two_factor_login_enabled": True}

    def test_super_admin_only_can_change(self, client, login_as, make_user):
        for role in (UserRole.USER, UserRole.ADMIN):
            login_as(make_user(role=role))
            assert self._put(client, False).status_code == 403
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        assert self._put(client, False).json() == {"two_factor_login_enabled": False}
        assert self._put(client, True).json() == {"two_factor_login_enabled": True}

    def test_switch_off_bypasses_challenge_and_back_on_restores(self, client,
                                                                db_session, login_as,
                                                                make_user, sent_codes):
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD,
                          two_factor_enabled=True)
        boss = make_user(role=UserRole.SUPER_ADMIN)

        # Gate on (default): challenge.
        assert _login(client, admin.email).json()["two_factor_required"] is True

        # Super admin flips the app-wide gate off -> straight to tokens,
        # even though the personal flag is still true.
        login_as(boss)
        assert self._put(client, False).status_code == 200
        body = _login(client, admin.email).json()
        assert "access_token" in body and "two_factor_required" not in body
        assert AuditAction.TWO_FACTOR_GLOBAL_TOGGLED in _actions(db_session)

        # Back on -> the stored personal flag applies again.
        assert self._put(client, True).status_code == 200
        assert _login(client, admin.email).json()["two_factor_required"] is True
