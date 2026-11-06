"""Manual trap counts: what field staff found when they emptied a trap.

One row per genus x sex per collection; a collection is (device, period_start,
period_end) — trap set → trap emptied, naive UTC like the rest of the DB.
Compared against the device's live AI detections over the same period.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# The genera the device model outputs (taxon_probs keys, see mqtt_client.py)
# plus "other" for anything else or unidentified.
GENERA = ("anopheles", "aedes", "culex", "other")
SEXES = ("female", "male", "unknown")


class ManualTrapCount(Base):
    __tablename__ = "manual_trap_counts"
    __table_args__ = (
        UniqueConstraint("device_id", "period_start", "period_end", "genus", "sex",
                         name="uq_manual_trap_counts_collection_group"),
        CheckConstraint("count >= 0", name="ck_manual_trap_counts_count_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    device_id: Mapped[int] = mapped_column(Integer, ForeignKey("devices.id", ondelete="CASCADE"))  # indexed by the unique constraint
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)
    genus: Mapped[str] = mapped_column(String(20))
    sex: Mapped[str] = mapped_column(String(10))
    count: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    entered_by_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
