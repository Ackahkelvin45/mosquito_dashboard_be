from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


class MosquitoCountDataPoint(BaseModel):
    """One bar in the mosquito count chart."""
    label: str = Field(..., description="Bucket label on the chart x-axis")
    count: int = Field(..., description="Mosquito count for this bucket")
    timestamp: datetime = Field(..., description="Start of the time bucket (UTC)")


class DashboardTotals(BaseModel):
    """
    Summary cards — scoped to the window defined by `totals_group_by`.
    """
    total_mosquito_count: int = Field(
        ..., description="Total mosquitoes detected within the totals window"
    )
    active_devices: int = Field(
        ..., description="Devices with ≥1 event or reading within the totals window"
    )
    inactive_devices: int = Field(
        ..., description="Devices with NO activity within the totals window"
    )
    total_devices: int = Field(..., description="Total registered devices (after device filters)")
    average_humidity: Optional[float] = Field(
        None, description="Average external humidity from readings within the totals window (%)"
    )
    average_internal_temp: Optional[float] = Field(
        None, description="Average internal temperature from readings within the totals window (°C)"
    )
    average_battery_voltage: Optional[float] = Field(
        None, description="Average battery voltage from readings within the totals window (V)"
    )
    total_regions_monitored: int = Field(
        ..., description="Distinct regions of devices active within the totals window"
    )
    # Echo back which window was used
    group_by: str = Field(..., description="Rolling window applied to these totals")
    window_start: datetime = Field(..., description="Totals window start (UTC)")
    window_end: datetime = Field(..., description="Totals window end (UTC)")


class DashboardChart(BaseModel):
    """
    Bar chart data — scoped to the window defined by `chart_group_by`.
    """
    data: List[MosquitoCountDataPoint] = Field(
        default=[], description="Time-bucketed mosquito counts"
    )
    total: int = Field(..., description="Sum of all counts in this chart window")
    # Echo back which window / bucket was used
    group_by: str = Field(..., description="Rolling window + bucket granularity applied to this chart")
    window_start: datetime = Field(..., description="Chart window start (UTC)")
    window_end: datetime = Field(..., description="Chart window end (UTC)")


class GenderDistribution(BaseModel):
    """
    Gender distribution of mosquitoes for a pie chart.
    """
    male: int = Field(0, description="Total male mosquitoes")
    female: int = Field(0, description="Total female mosquitoes")
    group_by: str = Field(..., description="Rolling window applied")
    window_start: datetime = Field(..., description="Window start (UTC)")
    window_end: datetime = Field(..., description="Window end (UTC)")


class CommunityMosquitoCountDataPoint(BaseModel):
    community: str = Field(..., description="Name of the community (monitored settlement)")
    count: int = Field(..., description="Mosquito count for this community")


class RegionMosquitoCountDataPoint(BaseModel):
    region: str = Field(..., description="Name of the region")
    count: int = Field(..., description="Mosquito count for this region (sum of its communities)")
    communities: List[CommunityMosquitoCountDataPoint] = Field(
        default=[],
        description="Per-community breakdown of this region's count, largest first. "
        "Lets the client draw each region bar as a stack of its communities.",
    )


class DashboardRegionChart(BaseModel):
    data: List[RegionMosquitoCountDataPoint] = Field(
        default=[], description="Region-bucketed mosquito counts"
    )
    communities: List[str] = Field(
        default=[],
        description="Every community present in this response, sorted alphabetically. "
        "A stable ordering the client can bind stack series and colours to, so a "
        "community keeps its colour when filters change the set of regions shown.",
    )
    group_by: str = Field(..., description="Rolling window applied")
    window_start: datetime = Field(..., description="Window start (UTC)")
    window_end: datetime = Field(..., description="Window end (UTC)")


class SensorStatusDataPoint(BaseModel):
    label: str = Field(..., description="Sample instant label on the chart x-axis")
    on_count: int = Field(..., description="Devices whose trap was ON at this instant (recent report)")
    off_count: int = Field(
        ..., description="Devices OFF at this instant — includes devices whose last report has gone stale"
    )
    timestamp: datetime = Field(..., description="Sample instant (UTC)")


class DashboardSensorStatusChart(BaseModel):
    """
    Line chart data for sensor statuses — scoped to `sensor_status_group_by`.

    Each point samples every device's state AT that instant: a device counts once
    as on/off using its latest reading at or before the instant (carried forward
    between reports). A device silent longer than the staleness window counts as
    OFF. Reporting frequency therefore never inflates the counts, and
    on + off == devices that have reported at least once by then.
    """
    data: List[SensorStatusDataPoint] = Field(
        default=[], description="Sampled per-instant device status counts"
    )
    group_by: str = Field(..., description="Rolling window applied")
    window_start: datetime = Field(..., description="Chart window start (UTC)")
    window_end: datetime = Field(..., description="Chart window end (UTC)")


class BreakdownItem(BaseModel):
    name: str = Field(..., description="Category name")
    count: int = Field(..., description="Mosquito count")


class DashboardBreakdown(BaseModel):
    """
    Breakdown of mosquito traits (sex, genus, species, age_group).
    """
    sex: List[BreakdownItem] = Field(default=[], description="Breakdown by sex")
    genus: List[BreakdownItem] = Field(default=[], description="Breakdown by genus")
    species: List[BreakdownItem] = Field(default=[], description="Breakdown by species")
    age_group: List[BreakdownItem] = Field(default=[], description="Breakdown by age group")
    group_by: str = Field(..., description="Rolling window applied")
    window_start: datetime = Field(..., description="Window start")
    window_end: datetime = Field(..., description="Window end")


class CorrelationDataPoint(BaseModel):
    """One time bucket pairing mosquito activity with ambient conditions."""
    label: str = Field(..., description="Bucket label on the chart x-axis")
    timestamp: datetime = Field(..., description="Start of the time bucket (UTC)")
    mosquito_count: int = Field(..., description="Mosquito count for this bucket")
    temperature: Optional[float] = Field(
        None, description="Average external temperature in this bucket (°C); null if no readings"
    )
    humidity: Optional[float] = Field(
        None, description="Average external humidity in this bucket (%); null if no readings"
    )


class DashboardCorrelationChart(BaseModel):
    """
    Correlation between mosquito counts and ambient temperature/humidity per bucket.
    `*_correlation` are Pearson coefficients in [-1, 1] over buckets where the
    respective sensor value exists; null when there are <2 points or zero variance.
    """
    data: List[CorrelationDataPoint] = Field(
        default=[], description="Per-bucket mosquito count alongside avg temperature/humidity"
    )
    temperature_correlation: Optional[float] = Field(
        None, description="Pearson r between mosquito_count and temperature"
    )
    humidity_correlation: Optional[float] = Field(
        None, description="Pearson r between mosquito_count and humidity"
    )
    group_by: str = Field(..., description="Rolling window + bucket granularity applied")
    window_start: datetime = Field(..., description="Window start (UTC)")
    window_end: datetime = Field(..., description="Window end (UTC)")


class GenusHeatmapCell(BaseModel):
    """A single genus × time-bucket cell in the heatmap grid."""
    genus: str = Field(..., description="Genus name (row)")
    label: str = Field(..., description="Time bucket label (column)")
    timestamp: datetime = Field(..., description="Start of the time bucket (UTC)")
    count: int = Field(..., description="Mosquito count for this genus in this bucket")


class DashboardGenusHeatmap(BaseModel):
    """
    Genus distribution over time as a dense heatmap grid.
    `data` contains one cell for every (genus × bucket) pair, including zeros.
    """
    genera: List[str] = Field(default=[], description="Distinct genera present (row axis)")
    buckets: List[str] = Field(default=[], description="Ordered time bucket labels (column axis)")
    data: List[GenusHeatmapCell] = Field(default=[], description="Full genus × bucket grid")
    group_by: str = Field(..., description="Rolling window + bucket granularity applied")
    window_start: datetime = Field(..., description="Window start (UTC)")
    window_end: datetime = Field(..., description="Window end (UTC)")


class DashboardResponse(BaseModel):
    """Unified dashboard — totals and chart each have their own independent window."""
    totals: DashboardTotals
    chart: DashboardChart
    gender_distribution: GenderDistribution
    region_chart: DashboardRegionChart
    sensor_status_chart: DashboardSensorStatusChart
    breakdown: DashboardBreakdown
    correlation_chart: DashboardCorrelationChart
    genus_heatmap: DashboardGenusHeatmap
    # Device filters echoed back
    region: Optional[str] = Field(None, description="Region filter applied")
    cluster_id: Optional[int] = Field(None, description="Cluster filter applied")
    device_id: Optional[int] = Field(None, description="Device filter applied")
