from pydantic import BaseModel
from typing import List, Optional, Dict
from datetime import datetime


class MosquitoCountPoint(BaseModel):
    label: str
    count: int
    timestamp: datetime


class MosquitoTrendPoint(BaseModel):
    label: str
    timestamp: datetime
    # Dynamic counts keyed by "{age_group}_{sex}" (e.g. "adult_male", "old_female").
    # Every point carries the same keys as the parent chart's `series_keys`.
    series: Dict[str, int] = {}


class MosquitoGenderPoint(BaseModel):
    label: str
    timestamp: datetime
    male: int = 0
    female: int = 0


class SensorStatusPoint(BaseModel):
    label: str
    timestamp: datetime
    on_count: int = 0
    off_count: int = 0


class TemperaturePoint(BaseModel):
    label: str
    timestamp: datetime
    external: Optional[float] = None
    internal: Optional[float] = None


class HumidityPoint(BaseModel):
    label: str
    timestamp: datetime
    external: Optional[float] = None
    internal: Optional[float] = None


class PressurePoint(BaseModel):
    label: str
    timestamp: datetime
    external: Optional[float] = None
    internal: Optional[float] = None


class BatteryPoint(BaseModel):
    label: str
    timestamp: datetime
    voltage: Optional[float] = None


class MosquitoCountChart(BaseModel):
    """AreaChart — total mosquito detections over time."""
    data: List[MosquitoCountPoint] = []
    group_by: str
    window_start: datetime
    window_end: datetime


class MosquitoTrendChart(BaseModel):
    """LineChart — mosquito counts split by age × sex combination.

    `series_keys` lists every "{age_group}_{sex}" combination present in the
    window, so the frontend knows which lines to render.
    """
    data: List[MosquitoTrendPoint] = []
    series_keys: List[str] = []
    group_by: str
    window_start: datetime
    window_end: datetime


class MosquitoGenderChart(BaseModel):
    """LineChart — male vs female mosquito counts over time."""
    data: List[MosquitoGenderPoint] = []
    group_by: str
    window_start: datetime
    window_end: datetime


class SensorStatusTrendChart(BaseModel):
    """LineChart — trap ON vs OFF readings per time bucket."""
    data: List[SensorStatusPoint] = []
    group_by: str
    window_start: datetime
    window_end: datetime


class TemperatureTrendChart(BaseModel):
    """LineChart — average external vs internal temperature per bucket."""
    data: List[TemperaturePoint] = []
    group_by: str
    window_start: datetime
    window_end: datetime


class HumidityTrendChart(BaseModel):
    """LineChart — average external vs internal humidity per bucket."""
    data: List[HumidityPoint] = []
    group_by: str
    window_start: datetime
    window_end: datetime


class PressureTrendChart(BaseModel):
    """LineChart — average external vs internal pressure per bucket."""
    data: List[PressurePoint] = []
    group_by: str
    window_start: datetime
    window_end: datetime


class BatteryTrendChart(BaseModel):
    """LineChart — average battery voltage per bucket."""
    data: List[BatteryPoint] = []
    group_by: str
    window_start: datetime
    window_end: datetime


class DeviceChartsResponse(BaseModel):
    mosquito_count: MosquitoCountChart
    mosquito_trend: MosquitoTrendChart
    mosquito_gender: MosquitoGenderChart
    sensor_status: SensorStatusTrendChart
    temperature: TemperatureTrendChart
    humidity: HumidityTrendChart
    pressure: PressureTrendChart
    battery: BatteryTrendChart
    group_by: str
    device_id: int
