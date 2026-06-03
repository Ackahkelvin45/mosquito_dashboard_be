from datetime import datetime
from app.crud.base import BaseRepository
from app.authentication.models import PasswordResetOTP


class PasswordResetRepository(BaseRepository[PasswordResetOTP]):
    model = PasswordResetOTP

    def create_otp(self, user_id: int, otp_code: str, expires_at: datetime) -> PasswordResetOTP:
        otp = PasswordResetOTP(user_id=user_id, otp_code=otp_code, expires_at=expires_at)
        self.session.add(otp)
        self.session.commit()
        self.session.refresh(otp)
        return otp

    def get_latest_active_otp(self, user_id: int) -> PasswordResetOTP | None:
        return (
            self.session.query(PasswordResetOTP)
            .filter(
                PasswordResetOTP.user_id == user_id,
                PasswordResetOTP.is_used == False,
            )
            .order_by(PasswordResetOTP.created_at.desc())
            .first()
        )

    def invalidate_user_otps(self, user_id: int) -> None:
        self.session.query(PasswordResetOTP).filter(
            PasswordResetOTP.user_id == user_id,
            PasswordResetOTP.is_used == False,
        ).update({PasswordResetOTP.is_used: True})
        self.session.commit()

    def mark_used(self, otp: PasswordResetOTP) -> None:
        otp.is_used = True
        self.session.commit()
