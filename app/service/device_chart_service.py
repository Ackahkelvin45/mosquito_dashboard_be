from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from app.device.models import MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading
from app.device.schema import ACTIVE_WINDOW_HOURS
from app.device.chart_schema import (
    MosquitoCountPoint, MosquitoTrendPoint, MosquitoGenderPoint,
    SensorStatusPoint, TemperaturePoint, HumidityPoint, PressurePoint, BatteryPoint,
    MosquitoCountChart, MosquitoTrendChart, MosquitoGenderChart,
    SensorStatusTrendChart, TemperatureTrendChart, HumidityTrendChart,
    PressureTrendChart, BatteryTrendChart, DeviceChartsResponse,
)

_WINDOW = {
    "hour":  timedelta(hours=1),
    "day":   timedelta(hours=24),
    "week":  timedelta(days=7),
    "month": timedelta(days=30),
    "year":  timedelta(days=365),
}

_BUCKET = {
    "hour":  (timedelta(minutes=1), "%H:%M"),
    "day":   (timedelta(hours=1),   "%H:00"),
    "week":  (timedelta(days=1),    "%Y-%m-%d"),
    "month": (timedelta(days=1),    "%Y-%m-%d"),
    "year":  (timedelta(days=30),   "%b %Y"),
}

VALID_GROUP_BY = set(_WINDOW.keys())


class DeviceChartService:
    def __init__(self, session: Session):
        self.session = session

    def get_device_charts(self, device_id: int, group_by: str = "month") -> DeviceChartsResponse:
        group_by = group_by.lower() if group_by in VALID_GROUP_BY else "month"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        window_start = now - _WINDOW[group_by]
        window_end = now

        sensor_readings = (
            self.session.query(SensorDeviceReading)
            .filter(
                SensorDeviceReading.device_id == device_id,
                SensorDeviceReading.timestamp >= window_start,
                SensorDeviceReading.timestamp <= window_end,
            )
            .all()
        )

        mosquito_events = (
            self.session.query(MosquitoEvent)
            .filter(
                MosquitoEvent.device_id == device_id,
                MosquitoEvent.timestamp >= window_start,
                MosquitoEvent.timestamp <= window_end,
            )
            .all()
        )

        events_with_readings = (
            self.session.query(MosquitoEvent, MosquitoIndividualReading)
            .join(MosquitoIndividualReading, MosquitoIndividualReading.batch_id == MosquitoEvent.id)
            .filter(
                MosquitoEvent.device_id == device_id,
                MosquitoEvent.timestamp >= window_start,
                MosquitoEvent.timestamp <= window_end,
            )
            .all()
        )

        return DeviceChartsResponse(
            mosquito_count=self._mosquito_count(mosquito_events, window_start, window_end, group_by),
            mosquito_trend=self._mosquito_trend(events_with_readings, window_start, window_end, group_by),
            mosquito_gender=self._mosquito_gender(events_with_readings, window_start, window_end, group_by),
            sensor_status=self._sensor_status(device_id, window_start, window_end, group_by),
            temperature=self._temperature(sensor_readings, window_start, window_end, group_by),
            humidity=self._humidity(sensor_readings, window_start, window_end, group_by),
            pressure=self._pressure(sensor_readings, window_start, window_end, group_by),
            battery=self._battery(sensor_readings, window_start, window_end, group_by),
            group_by=group_by,
            device_id=device_id,
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_buckets(self, window_start: datetime, window_end: datetime, group_by: str) -> list:
        bucket_delta, _ = _BUCKET[group_by]
        buckets = []
        cursor = window_start
        # `<` — a bucket starting AT window_end could never collect data and
        # would render as a false drop to zero at the chart's right edge.
        while cursor < window_end:
            buckets.append(cursor)
            cursor += bucket_delta
        return buckets

    def _bucket_key(self, ts: datetime, window_start: datetime, bucket_delta: timedelta) -> datetime:
        ts = ts.replace(tzinfo=None) if ts.tzinfo else ts
        n = int((ts - window_start).total_seconds() // bucket_delta.total_seconds())
        return window_start + n * bucket_delta

    def _avg(self, values: list) -> float | None:
        vals = [v for v in values if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def _bucket_sensor_readings(self, readings, window_start, window_end, group_by):
        bucket_delta, label_fmt = _BUCKET[group_by]
        buckets: dict = {ts: [] for ts in self._make_buckets(window_start, window_end, group_by)}
        for r in readings:
            key = self._bucket_key(r.timestamp, window_start, bucket_delta)
            if key in buckets:
                buckets[key].append(r)
        return sorted(buckets.items()), label_fmt

    # ── Chart 1: MosquitoCountChart ──────────────────────────────────────────

    def _mosquito_count(self, events, window_start, window_end, group_by) -> MosquitoCountChart:
        bucket_delta, label_fmt = _BUCKET[group_by]
        buckets: dict[datetime, int] = {ts: 0 for ts in self._make_buckets(window_start, window_end, group_by)}

        for event in events:
            key = self._bucket_key(event.timestamp, window_start, bucket_delta)
            if key in buckets:
                buckets[key] += event.count

        data = [
            MosquitoCountPoint(label=ts.strftime(label_fmt), count=cnt, timestamp=ts)
            for ts, cnt in sorted(buckets.items())
        ]
        return MosquitoCountChart(data=data, group_by=group_by, window_start=window_start, window_end=window_end)

    # ── Chart 2: MosquitoTrendChart ──────────────────────────────────────────

    def _mosquito_trend(self, events_with_readings, window_start, window_end, group_by) -> MosquitoTrendChart:
        bucket_delta, label_fmt = _BUCKET[group_by]
        buckets: dict[datetime, defaultdict] = {
            ts: defaultdict(int) for ts in self._make_buckets(window_start, window_end, group_by)
        }
        series_keys: set[str] = set()

        for event, reading in events_with_readings:
            key = self._bucket_key(event.timestamp, window_start, bucket_delta)
            if key in buckets:
                age = (reading.age_group or "").lower().strip() or "unknown"
                sex = (reading.sex or "").lower().strip() or "unknown"
                combo = f"{age}_{sex}"
                buckets[key][combo] += event.count
                series_keys.add(combo)

        ordered_keys = sorted(series_keys)
        data = [
            MosquitoTrendPoint(
                label=ts.strftime(label_fmt),
                timestamp=ts,
                series={k: cats.get(k, 0) for k in ordered_keys},
            )
            for ts, cats in sorted(buckets.items())
        ]
        return MosquitoTrendChart(
            data=data,
            series_keys=ordered_keys,
            group_by=group_by,
            window_start=window_start,
            window_end=window_end,
        )

    # ── Chart 3: MosquitoGenderChart ─────────────────────────────────────────

    def _mosquito_gender(self, events_with_readings, window_start, window_end, group_by) -> MosquitoGenderChart:
        bucket_delta, label_fmt = _BUCKET[group_by]
        buckets: dict[datetime, dict] = {
            ts: {"male": 0, "female": 0} for ts in self._make_buckets(window_start, window_end, group_by)
        }

        for event, reading in events_with_readings:
            key = self._bucket_key(event.timestamp, window_start, bucket_delta)
            if key in buckets:
                sex = (reading.sex or "").lower().strip()
                if sex in ("male", "female"):
                    buckets[key][sex] += event.count

        data = [
            MosquitoGenderPoint(
                label=ts.strftime(label_fmt),
                timestamp=ts,
                male=counts["male"],
                female=counts["female"],
            )
            for ts, counts in sorted(buckets.items())
        ]
        return MosquitoGenderChart(data=data, group_by=group_by, window_start=window_start, window_end=window_end)

    # ── Chart 4: ActiveStatusTrendChart ──────────────────────────────────────

    def _sensor_status(self, device_id, window_start, window_end, group_by) -> SensorStatusTrendChart:
        """Sample this device's trap state at each instant.

        trap_status is a STATE, not an event, so it is sampled — never counted.
        Counting rows made this a chart of how often the device reported (a
        30s-interval device produced ~120 per hourly bucket, and any bucket it
        skipped read as 0 even though the trap never changed). Now each point
        takes the latest reading at or before that instant, carried forward, so
        the line steps between 0 and 1. A reading older than
        ACTIVE_WINDOW_HOURS reads as OFF — a dark trap is not operating.
        """
        bucket_delta, label_fmt = _BUCKET[group_by]
        stale = timedelta(hours=ACTIVE_WINDOW_HOURS)

        # Sample instants span the window inclusively so the final point is the
        # state as of `window_end` (i.e. right now).
        samples: list[datetime] = []
        cursor = window_start
        while cursor <= window_end:
            samples.append(cursor)
            cursor += bucket_delta

        first_seen = (
            self.session.query(func.min(SensorDeviceReading.timestamp))
            .filter(SensorDeviceReading.device_id == device_id)
            .scalar()
        )
        if first_seen is not None and first_seen.tzinfo:
            first_seen = first_seen.replace(tzinfo=None)

        # Readings that can influence any sample. Anything older than `stale`
        # before the first instant reads as OFF regardless, so the lookback is
        # bounded rather than scanning full history.
        rows = (
            self.session.query(SensorDeviceReading.timestamp, SensorDeviceReading.trap_status)
            .filter(
                SensorDeviceReading.device_id == device_id,
                SensorDeviceReading.timestamp >= window_start - stale,
                SensorDeviceReading.timestamp <= window_end,
            )
            .order_by(SensorDeviceReading.timestamp, SensorDeviceReading.id)
            .all()
        )
        readings = [
            ((ts.replace(tzinfo=None) if ts.tzinfo else ts), bool(status))
            for ts, status in rows
        ]

        data: list[SensorStatusPoint] = []
        i = 0
        last: tuple[datetime, bool] | None = None
        for t in samples:
            while i < len(readings) and readings[i][0] <= t:
                last = readings[i]
                i += 1

            if first_seen is None or first_seen > t:
                # Device had not reported yet — state unknown, not "off".
                on = off = 0
            elif last is not None and (t - last[0]) <= stale and last[1]:
                on, off = 1, 0
            else:
                on, off = 0, 1

            data.append(
                SensorStatusPoint(
                    label=t.strftime(label_fmt),
                    timestamp=t,
                    on_count=on,
                    off_count=off,
                )
            )

        return SensorStatusTrendChart(data=data, group_by=group_by, window_start=window_start, window_end=window_end)

    # ── Chart 5: TemperatureTrendChart ───────────────────────────────────────

    def _temperature(self, readings, window_start, window_end, group_by) -> TemperatureTrendChart:
        bucketed, label_fmt = self._bucket_sensor_readings(readings, window_start, window_end, group_by)
        data = [
            TemperaturePoint(
                label=ts.strftime(label_fmt),
                timestamp=ts,
                external=self._avg([r.external_temperature for r in rs]),
                internal=self._avg([r.internal_temperature for r in rs]),
            )
            for ts, rs in bucketed
        ]
        return TemperatureTrendChart(data=data, group_by=group_by, window_start=window_start, window_end=window_end)

    # ── Chart 6: HumidityTrendChart ──────────────────────────────────────────

    def _humidity(self, readings, window_start, window_end, group_by) -> HumidityTrendChart:
        bucketed, label_fmt = self._bucket_sensor_readings(readings, window_start, window_end, group_by)
        data = [
            HumidityPoint(
                label=ts.strftime(label_fmt),
                timestamp=ts,
                external=self._avg([r.external_humidity for r in rs]),
                internal=self._avg([r.internal_humidity for r in rs]),
            )
            for ts, rs in bucketed
        ]
        return HumidityTrendChart(data=data, group_by=group_by, window_start=window_start, window_end=window_end)

    # ── Chart 7: PressureTrendChart ──────────────────────────────────────────

    def _pressure(self, readings, window_start, window_end, group_by) -> PressureTrendChart:
        bucketed, label_fmt = self._bucket_sensor_readings(readings, window_start, window_end, group_by)
        data = [
            PressurePoint(
                label=ts.strftime(label_fmt),
                timestamp=ts,
                external=self._avg([r.external_pressure for r in rs]),
                internal=self._avg([r.internal_pressure for r in rs]),
            )
            for ts, rs in bucketed
        ]
        return PressureTrendChart(data=data, group_by=group_by, window_start=window_start, window_end=window_end)

    # ── Chart 8: BatteryTrendChart ───────────────────────────────────────────

    def _battery(self, readings, window_start, window_end, group_by) -> BatteryTrendChart:
        bucketed, label_fmt = self._bucket_sensor_readings(readings, window_start, window_end, group_by)
        data = [
            BatteryPoint(
                label=ts.strftime(label_fmt),
                timestamp=ts,
                voltage=self._avg([r.battery_voltage for r in rs]),
            )
            for ts, rs in bucketed
        ]
        return BatteryTrendChart(data=data, group_by=group_by, window_start=window_start, window_end=window_end)
