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


class DataQualityDevice(BaseModel):
    """Per-device data-quality figures over the report window (live rows only).

    Counts are null when there is nothing to count them over — a device with
    no readings has no null-percentage, not 0%."""
    device_id: int
    device_uuid: Optional[str] = None
    name: str
    cluster_id: Optional[int] = None
    region: Optional[str] = None

    total_readings: int = Field(..., description="Live sensor readings in the window")
    test_readings: int = Field(..., description="Test-mode readings in the window (not counted anywhere else)")
    excluded_readings: int = Field(0, description="Live readings in the window an admin excluded from analysis (still counted in the figures here)")
    last_reading_at: Optional[datetime] = Field(None, description="Newest live reading in the window (device clock)")
    null_pct: dict[str, Optional[float]] = Field(
        ..., description="Per sensor field: % of live readings where it was null ('no valid reading'). Null when there are no readings.")
    temperature_out_of_range: int = Field(..., description="Readings whose temperature (external, else internal) is outside the alert range")
    humidity_out_of_range: int = Field(..., description="Readings whose humidity (external, else internal) is outside the alert range")
    battery_pct_out_of_range: int = Field(..., description="Readings with battery_pct outside 0-100")
    longest_silence_min: Optional[float] = Field(
        None, description="Longest time between two consecutive live readings in the window. Null with fewer than 2 readings.")
    silences_over_threshold: Optional[int] = Field(
        None, description="Silences longer than the offline threshold between consecutive live readings. Null with fewer than 2 readings.")
    delay_measured: int = Field(
        0, description="Live readings in the window that have a server receive time (received_at). Older and CSV-imported readings have none.")
    avg_delay_s: Optional[float] = Field(
        None, description="Mean of received_at - timestamp (server clock minus device clock), seconds. Null when nothing was measured.")
    min_delay_s: Optional[float] = Field(
        None, description="Smallest delay, seconds. Negative = device clock ahead of the server. Null when nothing was measured.")
    max_delay_s: Optional[float] = Field(None, description="Largest delay, seconds. Null when nothing was measured.")
    delayed_over_threshold: int = Field(
        0, description="Measured readings that arrived more than the offline threshold after their timestamp")

    detections: int = Field(..., description="Live mosquito events in the window")
    detections_missing_p_mosq: int
    detections_missing_genus: int
    detections_p_mosq_out_of_range: int = Field(..., description="p_mosq outside 0-1")
    excluded_detections: int = Field(0, description="Live mosquito events in the window an admin excluded from analysis (still counted in the figures here)")


class DataQualityReport(BaseModel):
    days: int
    window_start: datetime
    window_end: datetime
    offline_threshold_min: int = Field(..., description="Silence threshold used for silences_over_threshold (same as the offline alert)")
    temp_min: float
    temp_max: float
    humidity_min: float
    humidity_max: float
    devices: list[DataQualityDevice] = []
