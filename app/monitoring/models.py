"""MQTT ingest observability tables — the data behind the System Health page.

Design (TODO.md / System Health plan):
- successes are AGGREGATED per device+type+hour (`mqtt_traffic_hourly`) so
  volume stays bounded no matter how chatty the firmware's publish interval is;
- failures are stored RAW (`mqtt_ingest_errors`) because each one matters and
  they should be rare. Both are pruned by the cleanup job (30 d / 7 d default).

Unlike notifications (deduped by design), these tables count every message —
they exist precisely because the notification feed can't answer "how many".
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MqttTrafficHourly(Base):
    __tablename__ = "mqtt_traffic_hourly"
    __table_args__ = (
        UniqueConstraint("bucket_start", "device_id", "event_type", "is_test",
                         name="uq_mqtt_traffic_bucket"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # Hour bucket in UTC (minutes/seconds zeroed).
    bucket_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    device_id: Mapped[int] = mapped_column(Integer, ForeignKey("devices.id"), index=True)
    # "sensor_data" | "mosquito_data" — the base MQTT event type.
    event_type: Mapped[str] = mapped_column(String(20))
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self):
        return (f"MqttTrafficHourly(bucket={self.bucket_start}, device_id={self.device_id}, "
                f"type={self.event_type}, count={self.message_count})")


class MqttIngestError(Base):
    __tablename__ = "mqtt_ingest_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    # "invalid_payload" | "malformed_topic" | "unknown_device" |
    # "handler_error" | "broker_disconnected" | "empty_reading"
    error_type: Mapped[str] = mapped_column(String(30), index=True)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    device_uuid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def __repr__(self):
        return f"MqttIngestError(type={self.error_type}, at={self.occurred_at})"
