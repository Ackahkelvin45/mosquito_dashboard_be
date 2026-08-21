from app.core.database import Base
from sqlalchemy import Enum, Integer, String, DateTime, Boolean,ForeignKey
from sqlalchemy.orm import relationship, mapped_column, Mapped
from datetime import datetime
from app.authentication.enums import UserRole, ApprovalStatus,ResearcherRequestStatus
from app.device.models import DeviceCluster




class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # FR-4: mandatory for ADMIN/SUPER_ADMIN regardless of this flag (the
    # service computes "effective 2FA"); USERs opt in via /auth/me/two-factor.
    two_factor_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Bumped on password reset / role change / deactivation / 2FA enable —
    # tokens carry it as "ver", so a bump invalidates every existing session.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    approval_status: Mapped[ApprovalStatus] = mapped_column(Enum(ApprovalStatus), default=ApprovalStatus.PENDING)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.USER)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    # The single cluster this user belongs to (their membership). Distinct from
    # `clusters` below, which is the many-to-many admin link. Nullable: a user
    # can exist before being assigned to a cluster.
    cluster_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("device_clusters.id"), nullable=True)
    cluster: Mapped["DeviceCluster | None"] = relationship(
        "DeviceCluster",
        foreign_keys=[cluster_id],
        back_populates="users",
    )
    researcher_request: Mapped["ResearcherRequest | None"] = relationship(
        "ResearcherRequest",
        back_populates="user",
        uselist=False,
    )
    clusters: Mapped[list["DeviceCluster"]] = relationship(
        "DeviceCluster",
        secondary="cluster_admins",
        back_populates="cluster_admins"
    )
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
    




class ResearcherRequest(Base):
    __tablename__ = "researcher_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user: Mapped["User"] = relationship("User", back_populates="researcher_request")
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), unique=True)
    cluster: Mapped["DeviceCluster"] = relationship("DeviceCluster", back_populates="researcher_request")
    cluster_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("device_clusters.id"), unique=True, nullable=True)
    status: Mapped[ResearcherRequestStatus] = mapped_column(Enum(ResearcherRequestStatus), default=ResearcherRequestStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)



    def __repr__(self):
        return f"ResearcherRequest(id={self.id}, email={self.user.email}, first_name={self.user.first_name}, last_name={self.user.last_name})"




class PasswordResetOTP(Base):
    __tablename__ = "password_reset_otps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    otp_code: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    # Wrong guesses burn the code after MAX_OTP_ATTEMPTS — without this a
    # 6-digit code could be brute-forced for its whole validity window.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    user: Mapped["User"] = relationship("User")

    def __repr__(self):
        return f"PasswordResetOTP(id={self.id}, user_id={self.user_id}, expires_at={self.expires_at})"


class LoginOTP(Base):
    """2FA login challenge (FR-4). One live row per user (previous ones are
    invalidated on re-login). The code is stored bcrypt-hashed; the challenge
    token — which binds verify-2fa to the successful password check — is
    stored SHA-256-hashed and is what the row is looked up by."""
    __tablename__ = "login_otps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(255))
    challenge_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    # Resend cooldown anchor — resend always re-emails (a new code), never
    # silently reuses one the mailer may have failed to deliver.
    last_sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    user: Mapped["User"] = relationship("User")

    def __repr__(self):
        return f"LoginOTP(id={self.id}, user_id={self.user_id}, expires_at={self.expires_at})"
