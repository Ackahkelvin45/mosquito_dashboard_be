from pydantic import BaseModel, EmailStr,Field,field_validator,ConfigDict
from typing import Union,Optional
from .enums import UserRole, ApprovalStatus
from datetime import datetime



class UserBase(BaseModel):
    first_name: str = Field(...,min_length=2, max_length=50, description="First name of the user")
    last_name: str = Field(...,min_length=2, max_length=50, description="Last name of the user")
    email: EmailStr = Field(...,description="Email of the user")
    is_active: bool = Field(default=True, description="Whether the user is active")
    role: UserRole = Field(default=UserRole.USER, description="Role of the user")
    approval_status: ApprovalStatus = Field(default=ApprovalStatus.PENDING, description="Approval status of the user")



class UserCreate(UserBase):
    password: str = Field(...,min_length=8, max_length=50, description="Password of the user")

    @field_validator("password")
    @classmethod
    def validate_password(cls, value:str) -> str:
        if len(value) < 8:
            raise ValueError("Password must be at least 8 characters long")

        if not any(char.isdigit() for char in value):
            raise ValueError("Password must contain at least one number")

        if not any(char.isalpha() for char in value):
            raise ValueError("Password must contain at least one letter")

        if not any(char.isupper() for char in value):
            raise ValueError("Password must contain at least one uppercase letter")

        return value



class UserResponse(UserBase):
    model_config = ConfigDict(from_attributes=True)
    id: int = Field(...,description="ID of the user")
    created_at: datetime = Field(...,description="Created at of the user")
    updated_at: datetime = Field(...,description="Updated at of the user")

class UserUpdate(UserBase):
    id: int = Field(...,description="ID of the user")
    first_name: Optional[str] = Field(None,min_length=2, max_length=50, description="First name of the user")
    last_name: Optional[str] = Field(None,min_length=2, max_length=50, description="Last name of the user")
    email: Optional[EmailStr] = Field(None,description="Email of the user")
    is_active: Optional[bool] = Field(None,description="Whether the user is active")
    role: Optional[UserRole] = Field(None,description="Role of the user")
    approval_status: Optional[ApprovalStatus] = Field(None,description="Approval status of the user")


class UserLogin(BaseModel):
    email: EmailStr = Field(...,description="Email of the user")
    password: str = Field(...,min_length=8, max_length=50, description="Password of the user")

class UserLoginResponse(BaseModel):
    access_token: str = Field(...,description="Access token of the user")
    refresh_token: str = Field(...,description="Refresh token of the user")

class UserLogout(BaseModel):
    message: str = Field(...,description="Message of the user")
