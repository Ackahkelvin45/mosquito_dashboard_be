from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class SilentDevice(BaseModel):
    id: int
    name: str
    last_sensor_data_at: Optional[datetime] = Field(
        None, description="Null = the device has never sent sensor_data")


class MqttStatusResponse(BaseModel):
    """Everything the status-card row needs, in one round trip."""
    broker_connected: Optional[bool] = Field(
        None, description="True/False from the live client; null = unknown (API just restarted)")
    broker_state_since: Optional[datetime] = Field(
        None, description="When the current connected/disconnected state began")
    last_message_at: Optional[datetime] = Field(
        None, description="Last MQTT message received from ANY device (in-memory; falls back to the fleet's latest activity after a restart)")
    messages_24h: int = Field(..., description="Successfully stored messages in the last 24h")
    errors_24h: int = Field(..., description="Ingest failures in the last 24h (every occurrence, not deduped)")
    devices_total: int
    devices_reporting: int = Field(..., description="Devices with sensor_data inside the offline threshold")
    silent_devices: list[SilentDevice] = Field(
        default=[], description="Devices outside the threshold, most recently heard first (capped)")
    offline_threshold_min: int


class TrafficBucket(BaseModel):
    bucket_start: datetime
    sensor_count: int = 0
    mosquito_count: int = 0
    test_count: int = Field(0, description="Test-mode messages of either type")


class MqttErrorResponse(BaseModel):
    id: int
    occurred_at: datetime
    error_type: str
    topic: Optional[str] = None
    device_uuid: Optional[str] = None
    detail: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
