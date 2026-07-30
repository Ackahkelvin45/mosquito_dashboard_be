import math
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.device.models import Device, MosquitoEvent, SensorDeviceReading, MosquitoIndividualReading
from app.device.schema import ACTIVE_WINDOW_HOURS
from app.dashboard.schema import (
    DashboardTotals,
    DashboardChart,
    DashboardResponse,
    MosquitoCountDataPoint,
    GenderDistribution,
    DashboardRegionChart,
    RegionMosquitoCountDataPoint,
    CommunityMosquitoCountDataPoint,
    SensorStatusDataPoint,
    DashboardSensorStatusChart,
    BreakdownItem,
    DashboardBreakdown,
    CorrelationDataPoint,
    DashboardCorrelationChart,
    GenusHeatmapCell,
    DashboardGenusHeatmap,
)

# Rolling window for each group_by value
_WINDOW = {
    "day":   timedelta(hours=24),
    "month": timedelta(days=30),
    # 360, not 365: the year then splits into 12 equal 30-day buckets — a
    # 365-day window would end on a misleading 5-day final bucket.
    "year":  timedelta(days=360),
}

# Chart (bucket timedelta, x-axis label format) for each group_by value
_BUCKET = {
    "day":   (timedelta(hours=1),  "%H:00"),
    "month": (timedelta(days=1),   "%Y-%m-%d"),
    # Day kept in the label: 30-day buckets drift across calendar months, so
    # "%b %Y" alone can repeat and the heatmap keys its columns by label.
    "year":  (timedelta(days=30),  "%d %b %y"),
}

VALID_GROUP_BY = set(_WINDOW.keys())


def _resolve_bucket(group_by: str, window_start: datetime, window_end: datetime):
    """Bucket size + label format for a window. Known group_by values use the
    fixed table; a custom date range scales its buckets to the window span so
    charts always land on a readable number of points. Labels always include
    enough of the date to stay unique — the heatmap keys its columns by label.
    """
    if group_by in _BUCKET:
        return _BUCKET[group_by]
    span = window_end - window_start
    if span <= timedelta(days=2):
        return (timedelta(hours=1), "%d %b %H:%M")
    if span <= timedelta(days=90):
        return (timedelta(days=1), "%d %b %y")
    if span <= timedelta(days=400):
        return (timedelta(days=7), "%d %b %y")
    return (timedelta(days=30), "%d %b %y")


def _to_naive_utc(dt: datetime) -> datetime:
    """DB timestamps are naive UTC; normalise tz-aware inputs to match."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


class DashboardService:
    def __init__(self, session: Session):
        self.session = session

    def get_dashboard(
        self,
        totals_group_by: str = "month",
        chart_group_by: str = "month",
        gender_group_by: str = "month",
        region_group_by: str = "month",
        sensor_status_group_by: str = "month",
        breakdown_group_by: str = "month",
        correlation_group_by: str = "month",
        genus_heatmap_group_by: str = "month",
        region: Optional[str] = None,
        cluster_id: Optional[int] = None,
        device_id: Optional[int] = None,
        allowed_cluster_ids: Optional[set[int]] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> DashboardResponse:
        """
        Single entry point for all dashboard data.

        - `totals_group_by` → controls the rolling window for summary card stats
        - `chart_group_by`  → controls the rolling window + bucket size for the bar chart

        Both can be set independently.

        When `start_date` AND `end_date` are given, that fixed window overrides
        every group_by: all sections cover the same custom range and echo
        `group_by="custom"`.
        """
        totals_group_by = totals_group_by.lower() if totals_group_by in VALID_GROUP_BY else "month"
        chart_group_by  = chart_group_by.lower()  if chart_group_by  in VALID_GROUP_BY else "month"
        gender_group_by = gender_group_by.lower() if gender_group_by in VALID_GROUP_BY else "month"
        region_group_by = region_group_by.lower() if region_group_by in VALID_GROUP_BY else "month"
        sensor_status_group_by = sensor_status_group_by.lower() if sensor_status_group_by in VALID_GROUP_BY else "month"
        breakdown_group_by = breakdown_group_by.lower() if breakdown_group_by in VALID_GROUP_BY else "month"
        correlation_group_by = correlation_group_by.lower() if correlation_group_by in VALID_GROUP_BY else "month"
        genus_heatmap_group_by = genus_heatmap_group_by.lower() if genus_heatmap_group_by in VALID_GROUP_BY else "month"

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        device_q = self.session.query(Device)
        if region:
            device_q = device_q.filter(Device.region.ilike(f"%{region}%"))
        if cluster_id is not None:
            device_q = device_q.filter(Device.cluster_id == cluster_id)
        if device_id is not None:
            device_q = device_q.filter(Device.id == device_id)
        # Cluster scope from the caller's role. None => unrestricted (super admin).
        # A restricted caller only ever sees devices in their allowed clusters,
        # no matter what region/cluster_id/device_id they asked for.
        if allowed_cluster_ids is not None:
            device_q = device_q.filter(Device.cluster_id.in_(allowed_cluster_ids))

        all_devices = device_q.all()
        device_ids = [d.id for d in all_devices]

        # A device-scoping filter was requested → charts must honour the matched
        # set even when it is EMPTY (an empty match means empty charts, never a
        # silent fallback to the whole fleet). A cluster-restricted caller is
        # ALWAYS scoped, so their device_ids drive the charts too. Only an
        # unrestricted caller with no explicit filter gets None (query all).
        has_device_filter = bool(region) or cluster_id is not None or device_id is not None
        is_restricted = allowed_cluster_ids is not None
        scoped_ids: Optional[list[int]] = (
            device_ids if (has_device_filter or is_restricted) else None
        )

        # A complete custom range overrides every per-section rolling window.
        custom = start_date is not None and end_date is not None
        custom_start = _to_naive_utc(start_date) if custom else None
        custom_end = _to_naive_utc(end_date) if custom else None

        def section_window(group_by: str) -> tuple[datetime, datetime, str]:
            if custom:
                return custom_start, custom_end, "custom"
            return now - _WINDOW[group_by], now, group_by

        # ── Totals (own window) ──────────────────────────────────────────────
        t_start, t_end, t_gb = section_window(totals_group_by)
        totals  = self._compute_totals(all_devices, device_ids, t_start, t_end, t_gb)

        # ── Chart (own window) ───────────────────────────────────────────────
        c_start, c_end, c_gb = section_window(chart_group_by)
        chart   = self._compute_chart(scoped_ids, c_start, c_end, c_gb)

        # ── Gender Distribution (own window) ──
        g_start, g_end, g_gb = section_window(gender_group_by)
        gender_distribution = self._compute_gender_distribution(scoped_ids, g_start, g_end, g_gb)

        # ── Region Chart (own window) ───────────────────────────────
        r_start, r_end, r_gb = section_window(region_group_by)
        region_chart = self._compute_region_chart(scoped_ids, r_start, r_end, r_gb)

        # ── Sensor Status Chart (own window) ───────────────────────────
        ss_start, ss_end, ss_gb = section_window(sensor_status_group_by)
        sensor_status_chart = self._compute_sensor_status_chart(scoped_ids, ss_start, ss_end, ss_gb)

        # ── Breakdown (own window) ──────────────────────────────────────
        b_start, b_end, b_gb = section_window(breakdown_group_by)
        breakdown = self._compute_breakdown(scoped_ids, b_start, b_end, b_gb)

        # ── Correlation Chart (own window) ──────────────────────────────
        cor_start, cor_end, cor_gb = section_window(correlation_group_by)
        correlation_chart = self._compute_correlation_chart(scoped_ids, cor_start, cor_end, cor_gb)

        # ── Genus Heatmap (own window) ──────────────────────────────────
        gh_start, gh_end, gh_gb = section_window(genus_heatmap_group_by)
        genus_heatmap = self._compute_genus_heatmap(scoped_ids, gh_start, gh_end, gh_gb)

        return DashboardResponse(
            totals=totals,
            chart=chart,
            gender_distribution=gender_distribution,
            region_chart=region_chart,
            sensor_status_chart=sensor_status_chart,
            breakdown=breakdown,
            correlation_chart=correlation_chart,
            genus_heatmap=genus_heatmap,
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
        device_ids: Optional[list[int]],
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

        if device_ids is not None:
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
        device_ids: Optional[list[int]],
        window_start: datetime,
        window_end: datetime,
        group_by: str,
    ) -> DashboardRegionChart:
        # Grouped by region AND community so each region bar can be drawn as a
        # stack of the communities monitored inside it. The region total is the
        # sum of its communities, so the bar height is unchanged.
        result = (
            self.session.query(
                Device.region,
                Device.community,
                func.sum(MosquitoEvent.count)
            )
            .join(Device, MosquitoEvent.device_id == Device.id)
            .filter(
                MosquitoEvent.timestamp >= window_start,
                MosquitoEvent.timestamp <= window_end,
            )
        )

        if device_ids is not None:
            result = result.filter(MosquitoEvent.device_id.in_(device_ids))

        rows = result.group_by(Device.region, Device.community).all()

        # region -> community -> count. Several devices can sit in the same
        # community, so their counts accumulate rather than overwrite.
        by_region: dict[str, dict[str, int]] = {}
        for region, community, count in rows:
            if count is None:
                continue
            region_name = region if region else "Unknown"
            community_name = community if community else "Unknown"
            bucket = by_region.setdefault(region_name, {})
            bucket[community_name] = bucket.get(community_name, 0) + (count or 0)

        data = [
            RegionMosquitoCountDataPoint(
                region=region_name,
                count=sum(communities.values()),
                communities=[
                    CommunityMosquitoCountDataPoint(community=name, count=value)
                    # Largest first, name as tiebreak so equal counts stay stable.
                    for name, value in sorted(
                        communities.items(), key=lambda kv: (-kv[1], kv[0])
                    )
                ],
            )
            for region_name, communities in by_region.items()
        ]

        data.sort(key=lambda x: x.count, reverse=True)

        # Alphabetical, not by count: the client binds colours to this order, and
        # a rank-based order would repaint every community whenever a filter
        # changed the counts.
        all_communities = sorted(
            {c.community for point in data for c in point.communities}
        )

        return DashboardRegionChart(
            data=data,
            communities=all_communities,
            group_by=group_by,
            window_start=window_start,
            window_end=window_end,
        )

    # ── Sensor Status Chart ──────────────────────────────────────────────────

    def _compute_sensor_status_chart(
        self,
        device_ids: Optional[list[int]],
        window_start: datetime,
        window_end: datetime,
        group_by: str,
    ) -> DashboardSensorStatusChart:
        """Sample every device's trap state at each chart instant.

        trap_status is a STATE, not an event, so it must be sampled — never
        summed. Each device counts exactly once per point: on/off from its
        latest reading at or before that instant (carried forward between
        reports). A device whose last report is older than ACTIVE_WINDOW_HOURS
        counts as OFF — a trap that has gone dark is not operating, and without
        this a device that died while ON would stay "on" forever. This keeps
        chatty devices from outweighing quiet ones and makes
        on + off == devices that have reported at least once by then.
        """
        bucket_delta, label_fmt = _resolve_bucket(group_by, window_start, window_end)
        stale = timedelta(hours=ACTIVE_WINDOW_HOURS)

        # Sample instants span the window inclusively; the last point is the
        # state as of `window_end` (i.e. right now).
        samples: list[datetime] = []
        cursor = window_start
        while cursor <= window_end:
            samples.append(cursor)
            cursor += bucket_delta

        # When each device FIRST reported ever — a device only joins the chart
        # from that moment on (before it, it is absent, not "offline").
        first_q = self.session.query(
            SensorDeviceReading.device_id,
            func.min(SensorDeviceReading.timestamp),
        )
        if device_ids is not None:
            first_q = first_q.filter(SensorDeviceReading.device_id.in_(device_ids))
        first_seen = dict(first_q.group_by(SensorDeviceReading.device_id).all())

        # Readings that can influence any sample. Anything older than `stale`
        # before the earliest sample is offline regardless, so the horizon is
        # bounded — no need to scan the full history.
        readings_q = (
            self.session.query(
                SensorDeviceReading.device_id,
                SensorDeviceReading.timestamp,
                SensorDeviceReading.trap_status,
            )
            .filter(
                SensorDeviceReading.timestamp >= window_start - stale,
                SensorDeviceReading.timestamp <= window_end,
            )
            .order_by(
                SensorDeviceReading.device_id,
                SensorDeviceReading.timestamp,
                SensorDeviceReading.id,
            )
        )
        if device_ids is not None:
            readings_q = readings_q.filter(SensorDeviceReading.device_id.in_(device_ids))

        per_device: dict[int, list[tuple[datetime, bool]]] = {}
        for dev_id, ts, status in readings_q.all():
            ts = ts.replace(tzinfo=None) if ts.tzinfo else ts
            per_device.setdefault(dev_id, []).append((ts, bool(status)))

        counts = {t: {"on": 0, "off": 0} for t in samples}
        for dev_id, first_ts in first_seen.items():
            first_ts = first_ts.replace(tzinfo=None) if first_ts.tzinfo else first_ts
            rs = per_device.get(dev_id, [])
            i = 0
            last: tuple[datetime, bool] | None = None
            for t in samples:
                if first_ts > t:
                    continue  # device hadn't reported yet at this instant
                while i < len(rs) and rs[i][0] <= t:
                    last = rs[i]
                    i += 1
                if last is not None and (t - last[0]) <= stale and last[1]:
                    counts[t]["on"] += 1
                else:
                    # OFF by latest reading, or silent/stale — a dark trap is
                    # not operating.
                    counts[t]["off"] += 1

        data_points = [
            SensorStatusDataPoint(
                label=t.strftime(label_fmt),
                on_count=c["on"],
                off_count=c["off"],
                timestamp=t,
            )
            for t, c in sorted(counts.items())
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
        device_ids: Optional[list[int]],
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
            if device_ids is not None:
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

    # ── Correlation Chart ─────────────────────────────────────────────────────

    @staticmethod
    def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
        """Pearson correlation coefficient. Returns None when undefined
        (<2 points or zero variance in either variable)."""
        n = len(xs)
        if n < 2:
            return None
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_x2 = sum(x * x for x in xs)
        sum_y2 = sum(y * y for y in ys)
        numerator = n * sum_xy - sum_x * sum_y
        denominator_sq = (n * sum_x2 - sum_x * sum_x) * (n * sum_y2 - sum_y * sum_y)
        if denominator_sq <= 0:
            return None
        return round(numerator / math.sqrt(denominator_sq), 4)

    def _compute_correlation_chart(
        self,
        device_ids: Optional[list[int]],
        window_start: datetime,
        window_end: datetime,
        group_by: str,
    ) -> DashboardCorrelationChart:
        bucket_delta, label_fmt = _resolve_bucket(group_by, window_start, window_end)

        def bucket_key(ts: datetime) -> datetime:
            ts = ts.replace(tzinfo=None) if ts.tzinfo else ts
            n = int((ts - window_start).total_seconds() // bucket_delta.total_seconds())
            return window_start + n * bucket_delta

        # Ordered empty buckets. `<` — a bucket starting AT window_end could
        # never collect data and would render as a false drop to zero.
        counts: dict[datetime, int] = {}
        temps: dict[datetime, list] = {}
        hums: dict[datetime, list] = {}
        cursor = window_start
        while cursor < window_end:
            counts[cursor] = 0
            temps[cursor] = []
            hums[cursor] = []
            cursor += bucket_delta

        # Mosquito counts per bucket
        event_q = self.session.query(MosquitoEvent).filter(
            MosquitoEvent.timestamp >= window_start,
            MosquitoEvent.timestamp <= window_end,
        )
        if device_ids is not None:
            event_q = event_q.filter(MosquitoEvent.device_id.in_(device_ids))
        for event in event_q.all():
            key = bucket_key(event.timestamp)
            if key in counts:
                counts[key] += event.count

        # Sensor readings per bucket (external temperature & humidity)
        reading_q = self.session.query(SensorDeviceReading).filter(
            SensorDeviceReading.timestamp >= window_start,
            SensorDeviceReading.timestamp <= window_end,
        )
        if device_ids is not None:
            reading_q = reading_q.filter(SensorDeviceReading.device_id.in_(device_ids))
        for r in reading_q.all():
            key = bucket_key(r.timestamp)
            if key not in temps:
                continue
            if r.external_temperature is not None:
                temps[key].append(r.external_temperature)
            if r.external_humidity is not None:
                hums[key].append(r.external_humidity)

        data: list[CorrelationDataPoint] = []
        temp_x: list[float] = []
        temp_y: list[float] = []
        hum_x: list[float] = []
        hum_y: list[float] = []
        for ts in sorted(counts.keys()):
            count = counts[ts]
            temp_avg = round(sum(temps[ts]) / len(temps[ts]), 2) if temps[ts] else None
            hum_avg = round(sum(hums[ts]) / len(hums[ts]), 2) if hums[ts] else None
            data.append(
                CorrelationDataPoint(
                    label=ts.strftime(label_fmt),
                    timestamp=ts,
                    mosquito_count=count,
                    temperature=temp_avg,
                    humidity=hum_avg,
                )
            )
            # Only correlate buckets where the sensor value exists.
            if temp_avg is not None:
                temp_x.append(count)
                temp_y.append(temp_avg)
            if hum_avg is not None:
                hum_x.append(count)
                hum_y.append(hum_avg)

        return DashboardCorrelationChart(
            data=data,
            temperature_correlation=self._pearson(temp_x, temp_y),
            humidity_correlation=self._pearson(hum_x, hum_y),
            group_by=group_by,
            window_start=window_start,
            window_end=window_end,
        )

    # ── Genus Heatmap ───────────────────────────────────────────────────────────

    def _compute_genus_heatmap(
        self,
        device_ids: Optional[list[int]],
        window_start: datetime,
        window_end: datetime,
        group_by: str,
    ) -> DashboardGenusHeatmap:
        bucket_delta, label_fmt = _resolve_bucket(group_by, window_start, window_end)

        def bucket_key(ts: datetime) -> datetime:
            ts = ts.replace(tzinfo=None) if ts.tzinfo else ts
            n = int((ts - window_start).total_seconds() // bucket_delta.total_seconds())
            return window_start + n * bucket_delta

        # Ordered time buckets (column axis). `<` — see _compute_correlation_chart.
        bucket_list: list[datetime] = []
        cursor = window_start
        while cursor < window_end:
            bucket_list.append(cursor)
            cursor += bucket_delta
        bucket_set = set(bucket_list)

        q = (
            self.session.query(
                func.coalesce(MosquitoIndividualReading.genus, "Unknown"),
                MosquitoEvent.timestamp,
                MosquitoEvent.count,
            )
            .join(MosquitoEvent, MosquitoEvent.id == MosquitoIndividualReading.batch_id)
            .filter(
                MosquitoEvent.timestamp >= window_start,
                MosquitoEvent.timestamp <= window_end,
            )
        )
        if device_ids is not None:
            q = q.filter(MosquitoEvent.device_id.in_(device_ids))

        grid: dict[tuple, int] = {}
        genera: set[str] = set()
        for genus, ts, count in q.all():
            genus = (str(genus).strip() or "Unknown")
            key = bucket_key(ts)
            if key not in bucket_set:
                # ts maps outside the window's bucket range; skip defensively.
                continue
            genera.add(genus)
            grid[(genus, key)] = grid.get((genus, key), 0) + (count or 0)

        ordered_genera = sorted(genera)
        cells = [
            GenusHeatmapCell(
                genus=genus,
                label=ts.strftime(label_fmt),
                timestamp=ts,
                count=grid.get((genus, ts), 0),
            )
            for genus in ordered_genera
            for ts in bucket_list
        ]

        return DashboardGenusHeatmap(
            genera=ordered_genera,
            buckets=[ts.strftime(label_fmt) for ts in bucket_list],
            data=cells,
            group_by=group_by,
            window_start=window_start,
            window_end=window_end,
        )

    # ── Bar chart ─────────────────────────────────────────────────────────────

    def _compute_chart(
        self,
        device_ids: Optional[list[int]],
        window_start: datetime,
        window_end: datetime,
        group_by: str,
    ) -> DashboardChart:
        bucket_delta, label_fmt = _resolve_bucket(group_by, window_start, window_end)

        event_q = self.session.query(MosquitoEvent).filter(
            MosquitoEvent.timestamp >= window_start,
            MosquitoEvent.timestamp <= window_end,
        )
        if device_ids is not None:
            event_q = event_q.filter(MosquitoEvent.device_id.in_(device_ids))
        events = event_q.all()

        # Build ordered empty buckets. `<` — see _compute_correlation_chart.
        buckets: dict[datetime, int] = {}
        cursor = window_start
        while cursor < window_end:
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
