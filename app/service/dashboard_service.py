from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.device.models import Device, MosquitoEvent, SensorDeviceReading, MosquitoIndividualReading
from app.dashboard.schema import (
    DashboardTotals,
    DashboardChart,
    DashboardResponse,
    MosquitoCountDataPoint,
    GenderDistribution,
    DashboardRegionChart,
    RegionMosquitoCountDataPoint,
    SensorStatusDataPoint,
    DashboardSensorStatusChart,
    BreakdownItem,
    DashboardBreakdown,
)

# Rolling window for each group_by value
_WINDOW = {
    "hour":  timedelta(hours=1),
    "day":   timedelta(hours=24),
    "week":  timedelta(days=7),
    "month": timedelta(days=30),
}

# Chart (bucket timedelta, x-axis label format) for each group_by value
_BUCKET = {
    "hour":  (timedelta(minutes=1), "%H:%M"),
    "day":   (timedelta(hours=1),   "%H:00"),
    "week":  (timedelta(days=1),    "%Y-%m-%d"),
    "month": (timedelta(days=1),    "%Y-%m-%d"),
}

VALID_GROUP_BY = set(_WINDOW.keys())


class DashboardService:
    def __init__(self, session: Session):
        self.session = session

    def get_dashboard(
        self,
        totals_group_by: str = "week",
        chart_group_by: str = "week",
        gender_group_by: str = "week",
        region_group_by: str = "week",
        sensor_status_group_by: str = "week",
        breakdown_group_by: str = "week",
        region: Optional[str] = None,
        cluster_id: Optional[int] = None,
        device_id: Optional[int] = None,
    ) -> DashboardResponse:
        """
        Single entry point for all dashboard data.

        - `totals_group_by` → controls the rolling window for summary card stats
        - `chart_group_by`  → controls the rolling window + bucket size for the bar chart

        Both can be set independently.
        """
        totals_group_by = totals_group_by.lower() if totals_group_by in VALID_GROUP_BY else "week"
        chart_group_by  = chart_group_by.lower()  if chart_group_by  in VALID_GROUP_BY else "week"
        gender_group_by = gender_group_by.lower() if gender_group_by in VALID_GROUP_BY else "week"
        region_group_by = region_group_by.lower() if region_group_by in VALID_GROUP_BY else "week"
        sensor_status_group_by = sensor_status_group_by.lower() if sensor_status_group_by in VALID_GROUP_BY else "week"
        breakdown_group_by = breakdown_group_by.lower() if breakdown_group_by in VALID_GROUP_BY else "week"

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        device_q = self.session.query(Device)
        if region:
            device_q = device_q.filter(Device.region.ilike(f"%{region}%"))
        if cluster_id is not None:
            device_q = device_q.filter(Device.cluster_id == cluster_id)
        if device_id is not None:
            device_q = device_q.filter(Device.id == device_id)

        all_devices = device_q.all()
        device_ids = [d.id for d in all_devices]

        # ── Totals (own window) ──────────────────────────────────────────────
        t_end   = now
        t_start = now - _WINDOW[totals_group_by]
        totals  = self._compute_totals(all_devices, device_ids, t_start, t_end, totals_group_by)

        # ── Chart (own window) ───────────────────────────────────────────────
        c_end   = now
        c_start = now - _WINDOW[chart_group_by]
        chart   = self._compute_chart(device_ids, c_start, c_end, chart_group_by)

        # ── Gender Distribution (own window) ──
        g_end   = now
        g_start = now - _WINDOW[gender_group_by]
        gender_distribution = self._compute_gender_distribution(device_ids, g_start, g_end, gender_group_by)

        # ── Region Chart (own window) ───────────────────────────────
        r_end   = now
        r_start = now - _WINDOW[region_group_by]
        region_chart = self._compute_region_chart(device_ids, r_start, r_end, region_group_by)

        # ── Sensor Status Chart (own window) ───────────────────────────
        ss_end   = now
        ss_start = now - _WINDOW[sensor_status_group_by]
        sensor_status_chart = self._compute_sensor_status_chart(device_ids, ss_start, ss_end, sensor_status_group_by)

        # ── Breakdown (own window) ──────────────────────────────────────
        b_end   = now
        b_start = now - _WINDOW[breakdown_group_by]
        breakdown = self._compute_breakdown(device_ids, b_start, b_end, breakdown_group_by)

        return DashboardResponse(
            totals=totals,
            chart=chart,
            gender_distribution=gender_distribution,
            region_chart=region_chart,
            sensor_status_chart=sensor_status_chart,
            breakdown=breakdown,
            region=region,
            cluster_id=cluster_id,
            device_id=device_id,
        )

    # ── Totals ───────────────────────────────────────────────────────────────

    def _compute_totals(
        self,
        all_devices: list,
        device_ids: list[int],
        window_start: datetime,
        window_end: datetime,
        group_by: str,
    ) -> DashboardTotals:
        total_devices = len(all_devices)

        # Total mosquito count within the window
        total_mosquito_count = 0
        if device_ids:
            result = (
                self.session.query(func.coalesce(func.sum(MosquitoEvent.count), 0))
                .filter(
                    MosquitoEvent.device_id.in_(device_ids),
                    MosquitoEvent.timestamp >= window_start,
                    MosquitoEvent.timestamp <= window_end,
                )
                .scalar()
            )
            total_mosquito_count = result or 0

        # Active devices = had ≥1 mosquito event OR sensor reading in the window
        active_device_ids: set[int] = set()
        if device_ids:
            active_from_events = (
                self.session.query(MosquitoEvent.device_id)
                .filter(
                    MosquitoEvent.device_id.in_(device_ids),
                    MosquitoEvent.timestamp >= window_start,
                    MosquitoEvent.timestamp <= window_end,
                )
                .distinct()
                .all()
            )
            active_from_sensors = (
                self.session.query(SensorDeviceReading.device_id)
                .filter(
                    SensorDeviceReading.device_id.in_(device_ids),
                    SensorDeviceReading.timestamp >= window_start,
                    SensorDeviceReading.timestamp <= window_end,
                )
                .distinct()
                .all()
            )
            active_device_ids = (
                {r[0] for r in active_from_events} | {r[0] for r in active_from_sensors}
            )

        active_devices = len(active_device_ids)
        inactive_devices = total_devices - active_devices

        # Regions of active devices within the window
        active_objs = [d for d in all_devices if d.id in active_device_ids]
        total_regions = len({d.region for d in active_objs if d.region})

        # Averages from ALL sensor readings in the window (not just latest)
        avg_humidity = avg_temp = avg_battery = None
        if device_ids:
            readings = (
                self.session.query(SensorDeviceReading)
                .filter(
                    SensorDeviceReading.device_id.in_(device_ids),
                    SensorDeviceReading.timestamp >= window_start,
                    SensorDeviceReading.timestamp <= window_end,
                )
                .all()
            )
            if readings:
                humidities = [r.external_humidity       for r in readings if r.external_humidity       is not None]
                temps      = [r.internal_temperature    for r in readings if r.internal_temperature    is not None]
                batteries  = [r.battery_voltage         for r in readings if r.battery_voltage         is not None]

                avg_humidity = round(sum(humidities) / len(humidities), 2) if humidities else None
                avg_temp     = round(sum(temps)      / len(temps),      2) if temps      else None
                avg_battery  = round(sum(batteries)  / len(batteries),  2) if batteries  else None

        return DashboardTotals(
            total_mosquito_count=total_mosquito_count,
            active_devices=active_devices,
            inactive_devices=inactive_devices,
            total_devices=total_devices,
            average_humidity=avg_humidity,
            average_internal_temp=avg_temp,
            average_battery_voltage=avg_battery,
            total_regions_monitored=total_regions,
            group_by=group_by,
            window_start=window_start,
            window_end=window_end,
        )

    # ── Gender Distribution ──────────────────────────────────────────────────

    def _compute_gender_distribution(
        self,
        device_ids: list[int],
        window_start: datetime,
        window_end: datetime,
        group_by: str,
    ) -> GenderDistribution:
        result = (
            self.session.query(
                func.lower(MosquitoIndividualReading.sex),
                func.sum(MosquitoEvent.count)
            )
            .join(MosquitoEvent, MosquitoEvent.id == MosquitoIndividualReading.batch_id)
            .filter(
                MosquitoEvent.timestamp >= window_start,
                MosquitoEvent.timestamp <= window_end,
            )
        )
        
        if device_ids:
            result = result.filter(MosquitoEvent.device_id.in_(device_ids))
            
        rows = result.group_by(func.lower(MosquitoIndividualReading.sex)).all()

        male_count = 0
        female_count = 0
        
        for sex, count in rows:
            if sex == 'male':
                male_count = count or 0
            elif sex == 'female':
                female_count = count or 0
                
        return GenderDistribution(
            male=male_count, 
            female=female_count,
            group_by=group_by,
            window_start=window_start,
            window_end=window_end,
        )

    # ── Region Chart ──────────────────────────────────────────────────────────

    def _compute_region_chart(
        self,
        device_ids: list[int],
        window_start: datetime,
        window_end: datetime,
        group_by: str,
    ) -> DashboardRegionChart:
        result = (
            self.session.query(
                Device.region,
                func.sum(MosquitoEvent.count)
            )
            .join(Device, MosquitoEvent.device_id == Device.id)
            .filter(
                MosquitoEvent.timestamp >= window_start,
                MosquitoEvent.timestamp <= window_end,
            )
        )
        
        if device_ids:
            result = result.filter(MosquitoEvent.device_id.in_(device_ids))
            
        rows = result.group_by(Device.region).all()
        
        data = [
            RegionMosquitoCountDataPoint(
                region=region if region else "Unknown",
                count=count or 0
            )
            for region, count in rows if count is not None
        ]
        
        data.sort(key=lambda x: x.count, reverse=True)

        return DashboardRegionChart(
            data=data,
            group_by=group_by,
            window_start=window_start,
            window_end=window_end,
        )

    # ── Sensor Status Chart ──────────────────────────────────────────────────

    def _compute_sensor_status_chart(
        self,
        device_ids: list[int],
        window_start: datetime,
        window_end: datetime,
        group_by: str,
    ) -> DashboardSensorStatusChart:
        bucket_delta, label_fmt = _BUCKET[group_by]

        q = self.session.query(SensorDeviceReading).filter(
            SensorDeviceReading.timestamp >= window_start,
            SensorDeviceReading.timestamp <= window_end,
        )
        if device_ids:
            q = q.filter(SensorDeviceReading.device_id.in_(device_ids))
        readings = q.all()

        # Build ordered empty buckets
        buckets: dict[datetime, dict[str, int]] = {}
        cursor = window_start
        while cursor <= window_end:
            buckets[cursor] = {"on": 0, "off": 0}
            cursor += bucket_delta

        for r in readings:
            ts = r.timestamp.replace(tzinfo=None) if r.timestamp.tzinfo else r.timestamp
            n = int((ts - window_start).total_seconds() // bucket_delta.total_seconds())
            key = window_start + n * bucket_delta
            if key in buckets:
                if r.trap_status:
                    buckets[key]["on"] += 1
                else:
                    buckets[key]["off"] += 1

        data_points = [
            SensorStatusDataPoint(
                label=ts.strftime(label_fmt),
                on_count=counts["on"],
                off_count=counts["off"],
                timestamp=ts
            )
            for ts, counts in sorted(buckets.items())
        ]

        return DashboardSensorStatusChart(
            data=data_points,
            group_by=group_by,
            window_start=window_start,
            window_end=window_end,
        )

    # ── Breakdown ────────────────────────────────────────────────────────────

    def _compute_breakdown(
        self,
        device_ids: list[int],
        window_start: datetime,
        window_end: datetime,
        group_by: str,
    ) -> DashboardBreakdown:
        def get_breakdown_for_column(col):
            q = (
                self.session.query(
                    func.coalesce(col, "Unknown"),
                    func.sum(MosquitoEvent.count)
                )
                .join(MosquitoEvent, MosquitoEvent.id == MosquitoIndividualReading.batch_id)
                .filter(
                    MosquitoEvent.timestamp >= window_start,
                    MosquitoEvent.timestamp <= window_end,
                )
            )
            if device_ids:
                q = q.filter(MosquitoEvent.device_id.in_(device_ids))
                
            rows = q.group_by(col).all()
            data = [
                BreakdownItem(name=str(name).strip() or "Unknown", count=int(count))
                for name, count in rows if count is not None
            ]
            data.sort(key=lambda x: x.count, reverse=True)
            return data

        return DashboardBreakdown(
            sex=get_breakdown_for_column(MosquitoIndividualReading.sex),
            genus=get_breakdown_for_column(MosquitoIndividualReading.genus),
            species=get_breakdown_for_column(MosquitoIndividualReading.species),
            age_group=get_breakdown_for_column(MosquitoIndividualReading.age_group),
            group_by=group_by,
            window_start=window_start,
            window_end=window_end,
        )

    # ── Bar chart ─────────────────────────────────────────────────────────────

    def _compute_chart(
        self,
        device_ids: list[int],
        window_start: datetime,
        window_end: datetime,
        group_by: str,
    ) -> DashboardChart:
        bucket_delta, label_fmt = _BUCKET[group_by]

        event_q = self.session.query(MosquitoEvent).filter(
            MosquitoEvent.timestamp >= window_start,
            MosquitoEvent.timestamp <= window_end,
        )
        if device_ids:
            event_q = event_q.filter(MosquitoEvent.device_id.in_(device_ids))
        events = event_q.all()

        # Build ordered empty buckets
        buckets: dict[datetime, int] = {}
        cursor = window_start
        while cursor <= window_end:
            buckets[cursor] = 0
            cursor += bucket_delta

        # Distribute events into buckets
        for event in events:
            ts = event.timestamp.replace(tzinfo=None) if event.timestamp.tzinfo else event.timestamp
            n = int((ts - window_start).total_seconds() // bucket_delta.total_seconds())
            key = window_start + n * bucket_delta
            if key in buckets:
                buckets[key] += event.count

        data_points = [
            MosquitoCountDataPoint(label=ts.strftime(label_fmt), count=count, timestamp=ts)
            for ts, count in sorted(buckets.items())
        ]

        return DashboardChart(
            data=data_points,
            total=sum(p.count for p in data_points),
            group_by=group_by,
            window_start=window_start,
            window_end=window_end,
        )
