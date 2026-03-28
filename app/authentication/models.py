from app.core.database import Base
from sqlalchemy import Enum, Integer, String, DateTime, Boolean
from sqlalchemy.orm import relationship, mapped_column, Mapped
from datetime import datetime
from app.authentication.enums import UserRole, ApprovalStatus




class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    approval_status: Mapped[ApprovalStatus] = mapped_column(Enum(ApprovalStatus), default=ApprovalStatus.PENDING)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.USER)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    def __repr__(self):
        return f"User(id={self.id}, email={self.email}, first_name={self.first_name}, last_name={self.last_name})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"
    
    @property
    def is_admin(self):
        return self.role == UserRole.ADMIN
    
    @property
    def is_super_admin(self):
        return self.role == UserRole.SUPER_ADMIN
    
    @property
    def is_user(self):
        return self.role == UserRole.USER
    
    @property
    def is_approved(self):
        return self.approval_status == ApprovalStatus.APPROVED
    
    @property
    def is_rejected(self):
        return self.approval_status == ApprovalStatus.REJECTED
    
    @property
    def is_pending(self):
        return self.approval_status == ApprovalStatus.PENDING
    