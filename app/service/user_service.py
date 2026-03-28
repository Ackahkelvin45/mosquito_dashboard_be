from app.authentication.repository.userrepository import UserRepository
from app.authentication.schema import UserCreate, UserLogin, UserResponse, UserLoginResponse
from app.core.security.authhandler import AuthHandler
from app.core.security.hashHelper import HashHelper
from app.service.email_service import send_welcome_email
from sqlalchemy.orm import Session
from fastapi import HTTPException


class UserService:
    def __init__(self, session: Session):
        self.user_repository = UserRepository(session)
        self.auth_handler = AuthHandler()
        self.hash_helper = HashHelper()

    def create_user(self, user_data: UserCreate) -> UserResponse:
        if self.user_repository.user_exists_by_email(user_data.email):
            raise HTTPException(status_code=400, detail="User already exists,Please login")
        user_data.password = self.hash_helper.hash_password(user_data.password)
        user = self.user_repository.create_user(user_data)
        return UserResponse.model_validate(user)

    def login_user(self, login_data: UserLogin) -> UserLoginResponse:
        user = self.user_repository.get_user_by_email(login_data.email)
        if not user:
            raise HTTPException(status_code=400, detail="Invalid credentials")
        if not self.hash_helper.verify_password(password=login_data.password, hashed_password=user.hashed_password):
            raise HTTPException(status_code=400, detail="Invalid credentials")
        if not self.hash_helper.verify_password(password=login_data.password, hashed_password=user.hashed_password):
            raise HTTPException(status_code=400, detail="Invalid credentials")
        access_token = self.auth_handler.create_access_token(user.id)
        refresh_token = self.auth_handler.create_refresh_token(user.id)
        return UserLoginResponse(access_token=access_token, refresh_token=refresh_token)


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
        


    def get_users(self) -> list[UserResponse]:
        users = self.user_repository.get_all_users()
        return [UserResponse.model_validate(user) for user in users]
