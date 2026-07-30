import random
from datetime import datetime, timedelta

from app.authentication.repository.userrepository import UserRepository
from app.authentication.repository.password_reset_repository import PasswordResetRepository
from app.device.models import DeviceCluster
from app.authentication.schema import UserCreate, UserLogin, UserResponse, UserLoginResponse
from app.core.security.authhandler import AuthHandler
from app.core.security.hashHelper import HashHelper
from app.notification.events import NotificationEvent, emit
from app.service.email_service import send_welcome_email
from app.core.pagination import Page, paginate
from sqlalchemy.orm import Session
from fastapi import HTTPException


OTP_EXPIRY_MINUTES = 10


class UserService:
    def __init__(self, session: Session):
        self.session = session
        self.user_repository = UserRepository(session)
        self.password_reset_repository = PasswordResetRepository(session)
        self.auth_handler = AuthHandler()
        self.hash_helper = HashHelper()

    def create_user(self, user_data: UserCreate) -> UserResponse:
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
        return UserResponse.model_validate(user)

    def login_user(self, login_data: UserLogin) -> UserLoginResponse:
        user = self.user_repository.get_user_by_email(login_data.email)
        if not user:
            raise HTTPException(status_code=400, detail="Invalid credentials")
        if not self.hash_helper.verify_password(password=login_data.password, hashed_password=user.hashed_password):
            raise HTTPException(status_code=400, detail="Invalid credentials")
        access_token = self.auth_handler.create_access_token(user.id)
        refresh_token = self.auth_handler.create_refresh_token(user.id)
        user_id = user.id
        return UserLoginResponse(access_token=access_token, refresh_token=refresh_token, user_id=user_id)


    def refresh_token(self, refresh_token: str) -> UserLoginResponse:

        payload = self.auth_handler.verify_token(refresh_token, expected_type="refresh")
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        new_access_token = self.auth_handler.create_access_token(payload)
        new_refresh_token = self.auth_handler.create_refresh_token(payload)
        return UserLoginResponse(access_token=new_access_token, refresh_token=new_refresh_token)

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
        otp_code = f"{random.randint(0, 999999):06d}"
        hashed_otp = self.hash_helper.hash_password(otp_code)
        expires_at = datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
        # Invalidate any previously issued, unused OTPs before creating a new one.
        self.password_reset_repository.invalidate_user_otps(user.id)
        self.password_reset_repository.create_otp(user.id, hashed_otp, expires_at)
        emit(self.session, NotificationEvent.PASSWORD_RESET, user=user)
        return {"otp": otp_code, "first_name": user.first_name, "email": user.email}

    def verify_otp(self, email: str, otp: str) -> bool:
        user = self.user_repository.get_user_by_email(email)
        if not user:
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")
        otp_record = self.password_reset_repository.get_latest_active_otp(user.id)
        if not otp_record or otp_record.expires_at < datetime.now():
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")
        if not self.hash_helper.verify_password(password=otp, hashed_password=otp_record.otp_code):
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")
        return True

    def reset_password(self, email: str, otp: str, new_password: str) -> None:
        user = self.user_repository.get_user_by_email(email)
        if not user:
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")
        otp_record = self.password_reset_repository.get_latest_active_otp(user.id)
        if not otp_record or otp_record.expires_at < datetime.now():
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")
        if not self.hash_helper.verify_password(password=otp, hashed_password=otp_record.otp_code):
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")
        hashed_password = self.hash_helper.hash_password(new_password)
        self.user_repository.update_password(user, hashed_password)
        self.password_reset_repository.mark_used(otp_record)

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
