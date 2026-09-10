import hashlib
import secrets
from datetime import datetime, timedelta

from app.audit.models import AuditAction
from app.audit.recorder import audit
from app.authentication.enums import ApprovalStatus, UserRole
from app.authentication.models import LoginOTP
from app.authentication.repository.userrepository import UserRepository
from app.authentication.repository.password_reset_repository import PasswordResetRepository
from app.device.models import DeviceCluster
from app.authentication.schema import (
    TwoFactorChallengeResponse, UserCreate, UserLogin, UserResponse,
    UserLoginResponse, UserUpdate,
)
from app.core.security.authhandler import AuthHandler
from app.core.security.hashHelper import HashHelper
from app.notification.events import NotificationEvent, emit
from app.service.email_service import send_two_factor_code_email, send_welcome_email
from app.core.pagination import Page, paginate
from sqlalchemy.orm import Session
from fastapi import HTTPException


OTP_EXPIRY_MINUTES = 10
# Wrong guesses allowed before an OTP (reset or 2FA) is burned.
MAX_OTP_ATTEMPTS = 5
TWO_FACTOR_RESEND_COOLDOWN_SECONDS = 60
_GENERIC_2FA_ERROR = "Invalid or expired code"

# Constant-time-ish response for unknown emails: run a real bcrypt verify
# against this throwaway hash so response timing doesn't reveal whether an
# account exists.
_DUMMY_HASH = HashHelper().hash_password(secrets.token_urlsafe(16))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class UserService:
    def __init__(self, session: Session):
        self.session = session
        self.user_repository = UserRepository(session)
        self.password_reset_repository = PasswordResetRepository(session)
        self.auth_handler = AuthHandler()
        self.hash_helper = HashHelper()

    def create_user(self, user_data: UserCreate,
                    actor: "UserResponse | None" = None) -> UserResponse:
        if self.user_repository.user_exists_by_email(user_data.email):
            raise HTTPException(status_code=400, detail="User already exists,Please login")
        if user_data.cluster_id is not None:
            cluster = (
                self.session.query(DeviceCluster)
                .filter(DeviceCluster.id == user_data.cluster_id)
                .first()
            )
            if not cluster:
                raise HTTPException(status_code=404, detail=f"Cluster with id {user_data.cluster_id} not found")
        user_data.password = self.hash_helper.hash_password(user_data.password)
        user = self.user_repository.create_user(user_data)
        emit(self.session, NotificationEvent.USER_REGISTERED, user=user)
        # Audited in the service so the route's forced role/cluster downgrade
        # is captured as what was actually stored.
        audit(self.session, AuditAction.USER_CREATED,
              actor_user_id=actor.id if actor else None,
              actor_email=actor.email if actor else None,
              target_type="user", target_id=user.id,
              detail={"email": user.email, "role": str(user.role),
                      "cluster_id": user.cluster_id})
        return UserResponse.model_validate(user)

    def update_user(self, user_id: int, data: UserUpdate,
                    actor: "UserResponse | None" = None) -> UserResponse:
        user = self.user_repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Only fields the client actually sent — defaults never clobber.
        updates = data.model_dump(exclude_unset=True, exclude={"id"})

        new_email = updates.get("email")
        if new_email and new_email != user.email and self.user_repository.user_exists_by_email(new_email):
            raise HTTPException(status_code=409, detail="A user with this email already exists")

        new_cluster_id = updates.get("cluster_id")
        if new_cluster_id is not None:
            cluster = (
                self.session.query(DeviceCluster)
                .filter(DeviceCluster.id == new_cluster_id)
                .first()
            )
            if not cluster:
                raise HTTPException(status_code=404, detail=f"Cluster with id {new_cluster_id} not found")

        # What actually changes (for the audit trail): old -> new per field.
        changed = {
            field: {"from": getattr(user, field, None), "to": value}
            for field, value in updates.items()
            if getattr(user, field, None) != value
        }
        for field, value in updates.items():
            setattr(user, field, value)
        # A role change or deactivation must take effect NOW, not when the
        # target's current tokens happen to expire (a demoted or deactivated
        # user keeping admin tokens for a week is an escalation window).
        if any(k in changed for k in ("role", "is_active")):
            user.token_version = int(user.token_version or 0) + 1
        self.session.commit()
        self.session.refresh(user)
        if changed:
            audit(self.session, AuditAction.USER_UPDATED,
                  actor_user_id=actor.id if actor else None,
                  actor_email=actor.email if actor else None,
                  target_type="user", target_id=user.id,
                  detail={"changed": {k: {"from": str(v["from"]), "to": str(v["to"])}
                                      for k, v in changed.items()}})
        return UserResponse.model_validate(user)

    def login_user(self, login_data: UserLogin,
                   ip: str | None = None) -> "UserLoginResponse | TwoFactorChallengeResponse":
        user = self.user_repository.get_user_by_email(login_data.email)
        if not user:
            # Burn comparable CPU so timing doesn't reveal account existence.
            self.hash_helper.verify_password(password=login_data.password,
                                             hashed_password=_DUMMY_HASH)
            audit(self.session, AuditAction.LOGIN_FAILED,
                  actor_email=login_data.email, detail={"reason": "unknown_email"}, ip=ip)
            raise HTTPException(status_code=400, detail="Invalid credentials")
        if not self.hash_helper.verify_password(password=login_data.password, hashed_password=user.hashed_password):
            audit(self.session, AuditAction.LOGIN_FAILED, actor_user_id=user.id,
                  actor_email=user.email, detail={"reason": "wrong_password"}, ip=ip)
            raise HTTPException(status_code=400, detail="Invalid credentials")
        # Credentials alone must not admit a deactivated/unapproved account
        # (the api_access routes re-check this precisely because login
        # historically didn't — now it does).
        if not user.is_active or user.approval_status != ApprovalStatus.APPROVED:
            audit(self.session, AuditAction.LOGIN_FAILED, actor_user_id=user.id,
                  actor_email=user.email, detail={"reason": "inactive_or_unapproved"}, ip=ip)
            raise HTTPException(status_code=403, detail="Account is inactive or awaiting approval")

        # FR-4: admins always; USERs when they opted in.
        if user.two_factor_enabled or user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
            return self._start_two_factor_challenge(user, ip=ip)

        audit(self.session, AuditAction.LOGIN_SUCCESS, actor_user_id=user.id,
              actor_email=user.email, ip=ip)
        return self._issue_tokens(user)

    def _issue_tokens(self, user) -> UserLoginResponse:
        version = int(user.token_version or 0)
        return UserLoginResponse(
            access_token=self.auth_handler.create_access_token(user.id, version),
            refresh_token=self.auth_handler.create_refresh_token(user.id, version),
            user_id=user.id,
        )

    def _start_two_factor_challenge(self, user, ip: str | None = None) -> TwoFactorChallengeResponse:
        # One live challenge per user — a fresh login burns older ones.
        self.session.query(LoginOTP).filter(
            LoginOTP.user_id == user.id, LoginOTP.is_used.is_(False)
        ).update({"is_used": True})
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = secrets.token_urlsafe(32)
        row = LoginOTP(
            user_id=user.id,
            code_hash=self.hash_helper.hash_password(code),
            challenge_hash=_sha256(challenge),
            expires_at=datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES),
            last_sent_at=datetime.now(),
        )
        self.session.add(row)
        self.session.commit()
        # Synchronous on purpose: a mail failure must surface as an error the
        # user can see, not a 200 followed by a code that never arrives.
        try:
            send_two_factor_code_email(user.email, user.first_name, code)
        except Exception:
            row.is_used = True
            self.session.commit()
            raise HTTPException(status_code=503,
                                detail="Could not deliver your verification code — try again shortly")
        audit(self.session, AuditAction.TWO_FACTOR_SENT, actor_user_id=user.id,
              actor_email=user.email, ip=ip)
        return TwoFactorChallengeResponse(
            two_factor_required=True,
            two_factor_token=challenge,
            message=f"A verification code was sent to your email. It expires in {OTP_EXPIRY_MINUTES} minutes.",
        )

    def verify_two_factor(self, two_factor_token: str, code: str,
                          ip: str | None = None) -> UserLoginResponse:
        row = (
            self.session.query(LoginOTP)
            .filter(LoginOTP.challenge_hash == _sha256(two_factor_token),
                    LoginOTP.is_used.is_(False))
            .first()
        )
        # One generic message for every failure mode — no oracle.
        if row is None or row.expires_at < datetime.now():
            audit(self.session, AuditAction.TWO_FACTOR_FAILED,
                  actor_user_id=row.user_id if row else None,
                  detail={"reason": "missing_or_expired"}, ip=ip)
            raise HTTPException(status_code=400, detail=_GENERIC_2FA_ERROR)

        # Count the attempt BEFORE comparing, and burn the code at the cap —
        # re-logging-in mints a new challenge, so the cap can't be reset.
        row.attempts = (row.attempts or 0) + 1
        if row.attempts > MAX_OTP_ATTEMPTS:
            row.is_used = True
            self.session.commit()
            audit(self.session, AuditAction.TWO_FACTOR_FAILED, actor_user_id=row.user_id,
                  detail={"reason": "attempts_exceeded"}, ip=ip)
            raise HTTPException(status_code=400, detail=_GENERIC_2FA_ERROR)
        self.session.commit()

        if not self.hash_helper.verify_password(password=code, hashed_password=row.code_hash):
            audit(self.session, AuditAction.TWO_FACTOR_FAILED, actor_user_id=row.user_id,
                  detail={"reason": "wrong_code", "attempt": row.attempts}, ip=ip)
            raise HTTPException(status_code=400, detail=_GENERIC_2FA_ERROR)

        row.is_used = True
        self.session.commit()
        user = self.user_repository.get_user_by_id(row.user_id)
        if not user or not user.is_active:
            raise HTTPException(status_code=400, detail=_GENERIC_2FA_ERROR)
        audit(self.session, AuditAction.TWO_FACTOR_SUCCESS, actor_user_id=user.id,
              actor_email=user.email, ip=ip)
        audit(self.session, AuditAction.LOGIN_SUCCESS, actor_user_id=user.id,
              actor_email=user.email, detail={"via": "2fa"}, ip=ip)
        return self._issue_tokens(user)

    def resend_two_factor(self, two_factor_token: str, ip: str | None = None) -> dict:
        row = (
            self.session.query(LoginOTP)
            .filter(LoginOTP.challenge_hash == _sha256(two_factor_token),
                    LoginOTP.is_used.is_(False))
            .first()
        )
        if row is None or row.expires_at < datetime.now():
            raise HTTPException(status_code=400, detail=_GENERIC_2FA_ERROR)
        since_last = (datetime.now() - (row.last_sent_at or row.created_at)).total_seconds()
        if since_last < TWO_FACTOR_RESEND_COOLDOWN_SECONDS:
            raise HTTPException(status_code=429, detail="Please wait before requesting another code",
                                headers={"Retry-After": str(int(TWO_FACTOR_RESEND_COOLDOWN_SECONDS - since_last) or 1)})
        # A NEW code every resend — only its hash is stored, so the old one
        # cannot be re-sent; expiry and the send anchor move forward with it.
        code = f"{secrets.randbelow(1_000_000):06d}"
        row.code_hash = self.hash_helper.hash_password(code)
        row.expires_at = datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
        row.last_sent_at = datetime.now()
        self.session.commit()
        user = self.user_repository.get_user_by_id(row.user_id)
        try:
            send_two_factor_code_email(user.email, user.first_name, code)
        except Exception:
            raise HTTPException(status_code=503,
                                detail="Could not deliver your verification code — try again shortly")
        audit(self.session, AuditAction.TWO_FACTOR_SENT, actor_user_id=user.id,
              actor_email=user.email, detail={"resend": True}, ip=ip)
        return {"message": "A new verification code was sent."}

    def set_two_factor(self, user_id: int, enabled: bool, current_password: str,
                       ip: str | None = None) -> dict:
        user = self.user_repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        # Re-prove the password: a hijacked session must not be able to flip
        # the second factor. (Deliberately NOT exposed through UserUpdate —
        # an admin editing another user could otherwise disable their 2FA.)
        if not self.hash_helper.verify_password(password=current_password,
                                                hashed_password=user.hashed_password):
            raise HTTPException(status_code=400, detail="Invalid password")
        if not enabled and user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
            raise HTTPException(status_code=403,
                                detail="Two-factor authentication is mandatory for administrators")
        reauth_required = False
        if enabled and not user.two_factor_enabled:
            user.two_factor_enabled = True
            # Kill existing sessions so the next login actually exercises 2FA.
            user.token_version = int(user.token_version or 0) + 1
            reauth_required = True
        elif not enabled and user.two_factor_enabled:
            user.two_factor_enabled = False
        self.session.commit()
        audit(self.session,
              AuditAction.TWO_FACTOR_ENABLED if enabled else AuditAction.TWO_FACTOR_DISABLED,
              actor_user_id=user.id, actor_email=user.email, ip=ip)
        return {"two_factor_enabled": user.two_factor_enabled,
                "reauth_required": reauth_required}

    def update_me(self, user_id: int, first_name: str | None, last_name: str | None,
                  ip: str | None = None) -> UserResponse:
        """Self-service profile edit (names only — see ProfileUpdateRequest)."""
        user = self.user_repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        changed = {}
        if first_name is not None and first_name != user.first_name:
            changed["first_name"] = {"from": user.first_name, "to": first_name}
            user.first_name = first_name
        if last_name is not None and last_name != user.last_name:
            changed["last_name"] = {"from": user.last_name, "to": last_name}
            user.last_name = last_name
        self.session.commit()
        self.session.refresh(user)
        if changed:
            audit(self.session, AuditAction.PROFILE_UPDATED,
                  actor_user_id=user.id, actor_email=user.email,
                  target_type="user", target_id=user.id,
                  detail={"changed": changed}, ip=ip)
        return UserResponse.model_validate(user)

    def change_password(self, user_id: int, current_password: str,
                        new_password: str, ip: str | None = None) -> dict:
        """Self-service password change: re-proves the current password, then
        invalidates every existing session (token_version bump) — the caller
        must sign in again with the new password."""
        user = self.user_repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if not self.hash_helper.verify_password(password=current_password,
                                                hashed_password=user.hashed_password):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        if current_password == new_password:
            raise HTTPException(status_code=422,
                                detail="New password must be different from the current one")
        self.user_repository.update_password(user, self.hash_helper.hash_password(new_password))
        user.token_version = int(user.token_version or 0) + 1
        self.session.commit()
        audit(self.session, AuditAction.PASSWORD_CHANGED,
              actor_user_id=user.id, actor_email=user.email,
              target_type="user", target_id=user.id, ip=ip)
        return {"message": "Password changed. Please sign in again.",
                "reauth_required": True}

    def refresh_token(self, refresh_token: str) -> UserLoginResponse:
        payload = self.auth_handler.verify_token_payload(refresh_token, expected_type="refresh")
        user = self.user_repository.get_user_by_id(int(payload["sub"]))
        # Re-read the user: a deactivated account or a bumped token_version
        # (password reset, role change, 2FA enable) must not keep refreshing.
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        if int(payload.get("ver", 0)) != int(user.token_version or 0):
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        return self._issue_tokens(user)

    def get_user_by_id(self, user_id: int) -> UserResponse:
        user = self.user_repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return UserResponse.model_validate(user)
        


    def forgot_password(self, email: str) -> dict | None:
        user = self.user_repository.get_user_by_email(email)
        if not user:
            # Do not reveal whether the email exists.
            return None
        # secrets, not random: a Mersenne-Twister OTP is recoverable from
        # observed outputs — unacceptable for a credential.
        otp_code = f"{secrets.randbelow(1_000_000):06d}"
        hashed_otp = self.hash_helper.hash_password(otp_code)
        expires_at = datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
        # Invalidate any previously issued, unused OTPs before creating a new one.
        self.password_reset_repository.invalidate_user_otps(user.id)
        self.password_reset_repository.create_otp(user.id, hashed_otp, expires_at)
        emit(self.session, NotificationEvent.PASSWORD_RESET, user=user)
        audit(self.session, AuditAction.PASSWORD_RESET_REQUESTED,
              actor_user_id=user.id, actor_email=user.email)
        return {"otp": otp_code, "first_name": user.first_name, "email": user.email}

    def _check_reset_otp(self, email: str, otp: str):
        """Shared verify for verify_otp/reset_password: counts every attempt
        and burns the code past MAX_OTP_ATTEMPTS (previously a reset code was
        unlimited-guess for its whole window). One generic error throughout."""
        user = self.user_repository.get_user_by_email(email)
        if not user:
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")
        otp_record = self.password_reset_repository.get_latest_active_otp(user.id)
        if not otp_record or otp_record.expires_at < datetime.now():
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")
        otp_record.attempts = (otp_record.attempts or 0) + 1
        if otp_record.attempts > MAX_OTP_ATTEMPTS:
            otp_record.is_used = True
            self.session.commit()
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")
        self.session.commit()
        if not self.hash_helper.verify_password(password=otp, hashed_password=otp_record.otp_code):
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")
        return user, otp_record

    def verify_otp(self, email: str, otp: str) -> bool:
        self._check_reset_otp(email, otp)
        return True

    def reset_password(self, email: str, otp: str, new_password: str) -> None:
        user, otp_record = self._check_reset_otp(email, otp)
        hashed_password = self.hash_helper.hash_password(new_password)
        self.user_repository.update_password(user, hashed_password)
        # A reset invalidates every existing session — a stolen refresh token
        # must not survive the victim recovering their account.
        user.token_version = int(user.token_version or 0) + 1
        self.session.commit()
        self.password_reset_repository.mark_used(otp_record)
        audit(self.session, AuditAction.PASSWORD_RESET_COMPLETED,
              actor_user_id=user.id, actor_email=user.email)

    def get_users(self,
                  page: int = 1,
                  page_size: int = 20,
                  email: str = None,
                  name: str = None,
                  role: str = None,
                  approval_status: str = None,
                  restrict_cluster_id: int = None,
                  ) -> Page[UserResponse]:
        if any(v is not None for v in [email, name, role, approval_status]):
            users = self.user_repository.filter_users(email=email, name=name, role=role, approval_status=approval_status)
        else:
            users = self.user_repository.get_all_users()

        # Cluster admins only see users within their own cluster.
        if restrict_cluster_id is not None:
            users = [u for u in users if u.cluster_id == restrict_cluster_id]

        sliced, total, total_pages = paginate(users, page, page_size)
        return Page[UserResponse](
            items=[UserResponse.model_validate(user) for user in sliced],
            total=total, page=page, page_size=page_size, total_pages=total_pages,
        )
