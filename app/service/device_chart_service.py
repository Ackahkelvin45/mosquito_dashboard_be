from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from app.device.models import MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading
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
            sensor_status=self._sensor_status(sensor_readings, window_start, window_end, group_by),
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
        while cursor <= window_end:
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

    def _sensor_status(self, readings, window_start, window_end, group_by) -> SensorStatusTrendChart:
        bucket_delta, label_fmt = _BUCKET[group_by]
        buckets: dict[datetime, dict] = {
            ts: {"on": 0, "off": 0} for ts in self._make_buckets(window_start, window_end, group_by)
        }

        for r in readings:
            key = self._bucket_key(r.timestamp, window_start, bucket_delta)
            if key in buckets:
                if r.trap_status:
                    buckets[key]["on"] += 1
                else:
                    buckets[key]["off"] += 1

        data = [
            SensorStatusPoint(
                label=ts.strftime(label_fmt),
                timestamp=ts,
                on_count=counts["on"],
                off_count=counts["off"],
            )
            for ts, counts in sorted(buckets.items())
        ]
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
