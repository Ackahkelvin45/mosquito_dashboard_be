"""FR-4 (email-OTP 2FA + session versioning), FR-27 (audit log), and the
security pre-fixes: login gates, OTP attempt caps, researcher-request RBAC,
refresh hardening."""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI

import app.service.user_service as user_service_module
from app.audit.models import AuditAction, AuditLog
from app.authentication.enums import ApprovalStatus, UserRole
from app.authentication.models import LoginOTP
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
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD)
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
        admin = make_user(role=UserRole.SUPER_ADMIN, password=PASSWORD)
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
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD)
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
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD)
        challenge = _login(client, admin.email).json()["two_factor_token"]
        db_session.query(LoginOTP).update(
            {"expires_at": datetime.now() - timedelta(minutes=1)})
        db_session.commit()
        res = client.post("/auth/login/verify-2fa",
                          json={"two_factor_token": challenge, "code": sent_codes[0][1]})
        assert res.status_code == 400

    def test_resend_cooldown_then_new_code(self, client, db_session, make_user,
                                           sent_codes):
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD)
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

    def test_admin_cannot_disable(self, client, db_session, make_user, sent_codes):
        admin = make_user(role=UserRole.ADMIN, password=PASSWORD)
        challenge = _login(client, admin.email).json()["two_factor_token"]
        token = client.post("/auth/login/verify-2fa",
                            json={"two_factor_token": challenge,
                                  "code": sent_codes[0][1]}).json()["access_token"]
        res = client.post("/auth/me/two-factor",
                          json={"enabled": False, "current_password": PASSWORD},
                          headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 403


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
