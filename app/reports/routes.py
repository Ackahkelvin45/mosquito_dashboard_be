"""Surveillance report generator — GET /reports/surveillance.

A fixed template (summary, detections, environment, alerts, data quality,
manual vs AI) over one period. Only measured figures: every section is built
from live rows (LIVE_SENSOR_READING / LIVE_MOSQUITO_EVENT; CSV-imported rows
count, test-mode rows never do) and a figure with nothing behind it is null
or an empty list, never 0-filled or estimated.

Periods (naive UTC, half-open [start, end)):
  * weekly  = the 7 days ending on `end` (inclusive); previous = the 7 before.
  * monthly = the calendar month containing `end`; previous = the calendar
    month before. A month still in progress is reported as far as it goes
    (`period_complete` = false), so compare it with care.

Scope: visible_cluster_ids (own cluster + public; super admin all), narrowed
by optional cluster_id / region. Non-guest users only. Alerts and data quality
are management views and follow data_quality.report_scope instead: own cluster
only (every non-super role, USER included), super admin all — public clusters'
alerts/silences are never shown to outsiders.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.core.security.permissions import effective_cluster_scope
from app.dashboard.schema import BreakdownItem, RegionMosquitoCountDataPoint
from app.device.models import (
    LIVE_MOSQUITO_EVENT, LIVE_SENSOR_READING, Device, DeviceCluster, MosquitoEvent,
    SensorDeviceReading,
)
from app.jobs.summaries import detection_stats
from app.monitoring.data_quality import build_report as build_data_quality, report_scope
from app.monitoring.model_performance import COLLECTION_RULE
from app.notification.enums import HANDLEABLE_TYPES
from app.notification.models import Notification
from app.service.dashboard_service import DashboardService
from app.trap_counts.models import ManualTrapCount
from app.trap_counts.routes import compare_collections, require_member

router = APIRouter()

Period = Literal["weekly", "monthly"]


# ── Schemas ──────────────────────────────────────────────────────────────────

class Summary(BaseModel):
    detections: int            # sum of live event counts (same as the dashboard total)
    detection_events: int
    # True when no device sent anything: detections=0 is then "not measured".
    no_device_reported: bool
    previous_detections: int
    previous_devices_reporting: int
    # Null while the period is still running (partial vs full is no comparison);
    # change_pct also null when the previous period had 0.
    change: Optional[int] = None
    change_pct: Optional[float] = None
    top_species: Optional[str] = None
    # Sent >= 1 live reading or detection in the period (CSV-imported rows count).
    devices_reporting: int
    devices_total: int         # devices in scope that existed before the period ended
    regions_covered: list[str]


class DailyPoint(BaseModel):
    date: date
    devices_reporting: int     # devices with a live reading or detection that day
    count: Optional[int] = None  # null when no device reported that day


class Detections(BaseModel):
    by_genus: list[BreakdownItem]
    by_sex: list[BreakdownItem]
    by_species: list[BreakdownItem]
    by_region: list[RegionMosquitoCountDataPoint]
    daily: list[DailyPoint]    # days up to now only
    unclassified: int          # detections with no AI classification row — not in the breakdowns


class Measure(BaseModel):
    readings: int
    mean: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None


class Environment(BaseModel):
    readings: int              # live sensor readings in the period
    temperature: Measure       # external, else internal (as events.py)
    humidity: Measure


class AlertType(BaseModel):
    type: str
    count: int
    handled: int


class Alerts(BaseModel):
    total: int
    handled: int
    by_type: list[AlertType]
    ungrouped_rows: int        # legacy rows without alert_group: counted per recipient row
    note: str


class DeviceIssue(BaseModel):
    device_id: int
    name: str
    region: Optional[str] = None
    longest_silence_min: Optional[float] = None
    silences_over_threshold: Optional[int] = None


class DataQuality(BaseModel):
    offline_threshold_min: int
    devices_checked: int
    no_readings: list[DeviceIssue]
    long_silences: list[DeviceIssue]


class ManualVsAi(BaseModel):
    collections: int
    collections_compared: int
    collections_not_reporting: int
    manual_total: int
    ai_total: int
    abs_difference: int
    rule: str
    message: Optional[str] = None


class SurveillanceReport(BaseModel):
    generated_at: datetime
    period: Period
    period_start: datetime
    period_end: datetime       # exclusive
    period_complete: bool
    previous_start: datetime
    previous_end: datetime
    scope: str
    management_scope: str      # scope of the alerts + data-quality sections
    cluster_id: Optional[int] = None
    region: Optional[str] = None
    summary: Summary
    detections: Detections
    environment: Environment
    alerts: Alerts
    data_quality: DataQuality
    manual_vs_ai: ManualVsAi


# ── Period ───────────────────────────────────────────────────────────────────

def period_bounds(period: Period, end: date) -> tuple[datetime, datetime]:
    if period == "weekly":
        stop = datetime.combine(end + timedelta(days=1), time())
        return stop - timedelta(days=7), stop
    start = datetime(end.year, end.month, 1)
    return start, datetime(end.year + end.month // 12, end.month % 12 + 1, 1)


def previous_bounds(period: Period, start: datetime) -> tuple[datetime, datetime]:
    return period_bounds(period, (start - timedelta(days=1)).date())


# ── Sections ─────────────────────────────────────────────────────────────────

def _reporting_ids(session: Session, ids: list[int], start: datetime, stop: datetime) -> set[int]:
    if not ids:
        return set()
    sensors = session.query(SensorDeviceReading.device_id).filter(
        SensorDeviceReading.device_id.in_(ids), SensorDeviceReading.timestamp >= start,
        SensorDeviceReading.timestamp < stop, LIVE_SENSOR_READING).distinct()
    events = session.query(MosquitoEvent.device_id).filter(
        MosquitoEvent.device_id.in_(ids), MosquitoEvent.timestamp >= start,
        MosquitoEvent.timestamp < stop, LIVE_MOSQUITO_EVENT).distinct()
    return {i for (i,) in sensors.union(events).all()}


def _daily(session: Session, ids: list[int], start: datetime, stop: datetime) -> list[DailyPoint]:
    """Per-day mosquito total and reporting devices. A day nobody reported on
    has count=None — not 0 (the dashboard chart zero-fills; a report must not)."""
    if not ids:
        return []
    sday, eday = func.date(SensorDeviceReading.timestamp), func.date(MosquitoEvent.timestamp)
    sensors = session.query(sday, SensorDeviceReading.device_id).filter(
        SensorDeviceReading.device_id.in_(ids), SensorDeviceReading.timestamp >= start,
        SensorDeviceReading.timestamp < stop, LIVE_SENSOR_READING).distinct().all()
    events = session.query(eday, MosquitoEvent.device_id, func.sum(MosquitoEvent.count)).filter(
        MosquitoEvent.device_id.in_(ids), MosquitoEvent.timestamp >= start,
        MosquitoEvent.timestamp < stop, LIVE_MOSQUITO_EVENT).group_by(eday, MosquitoEvent.device_id).all()
    devices: dict = defaultdict(set)
    counts: dict = defaultdict(int)
    for d, dev in sensors:
        devices[str(d)].add(dev)  # SQLite returns a string, Postgres a date
    for d, dev, n in events:
        devices[str(d)].add(dev)
        counts[str(d)] += int(n or 0)
    points, day = [], start.date()
    while day < stop.date():
        key = day.isoformat()
        n = len(devices.get(key, ()))
        points.append(DailyPoint(date=day, devices_reporting=n, count=counts[key] if n else None))
        day += timedelta(days=1)
    return points


def _environment(session: Session, ids: list[int], start: datetime, stop: datetime) -> Environment:
    temp = func.coalesce(SensorDeviceReading.external_temperature, SensorDeviceReading.internal_temperature)
    hum = func.coalesce(SensorDeviceReading.external_humidity, SensorDeviceReading.internal_humidity)
    row = (0, 0, None, None, None, 0, None, None, None)
    if ids:
        row = session.query(
            func.count(SensorDeviceReading.id),
            func.count(temp), func.avg(temp), func.min(temp), func.max(temp),
            func.count(hum), func.avg(hum), func.min(hum), func.max(hum),
        ).filter(SensorDeviceReading.device_id.in_(ids), SensorDeviceReading.timestamp >= start,
                 SensorDeviceReading.timestamp < stop, LIVE_SENSOR_READING).one()

    def measure(n, mean, lo, hi):
        r = lambda v: round(float(v), 2) if v is not None else None  # noqa: E731
        return Measure(readings=n or 0, mean=r(mean), min=r(lo), max=r(hi))

    return Environment(readings=row[0] or 0, temperature=measure(*row[1:5]), humidity=measure(*row[5:9]))


def _alerts(session: Session, ids: list[int], start: datetime, stop: datetime) -> Alerts:
    rows = session.query(
        Notification.id, Notification.notification_type, Notification.alert_group,
        Notification.handled_at,
    ).filter(
        Notification.device_id.in_(ids), Notification.created_at >= start,
        Notification.created_at < stop, Notification.notification_type.in_(HANDLEABLE_TYPES),
    ).all() if ids else []
    # One emission fans out one row per recipient; alert_group links them and
    # handled_at is shared across the group.
    groups: dict = {}
    for nid, ntype, group, handled_at in rows:
        key = group or f"row:{nid}"
        prev = groups.get(key)
        groups[key] = (str(ntype), bool(handled_at) or (prev is not None and prev[1]))
    by_type: dict = defaultdict(lambda: [0, 0])
    for ntype, handled in groups.values():
        by_type[ntype][0] += 1
        by_type[ntype][1] += handled
    return Alerts(
        total=len(groups),
        handled=sum(h for _, h in groups.values()),
        by_type=sorted((AlertType(type=t, count=c, handled=h) for t, (c, h) in by_type.items()),
                       key=lambda a: (-a.count, a.type)),
        ungrouped_rows=sum(1 for r in rows if not r[2]),
        note=("Delivered alerts: an alert exists only if it was sent to at least one recipient, "
              "which depends on recipients' notification preferences. "
              "Counted once per emission (alert_group), not per recipient. "
              "Rows created before alert_group existed are counted per recipient row. "
              "Alerts not tied to a device (e.g. pipeline silent) are not included."),
    )


def _data_quality(session: Session, scope: Optional[set[int]], ids: list[int],
                  start: datetime, stop: datetime, now: datetime) -> DataQuality:
    end = min(stop, now)  # an in-progress period ends now, not in the future
    report = build_data_quality(session, scope, (stop - start).days, now=end, start=start)
    wanted = set(ids)
    devices = [d for d in report.devices if d.device_id in wanted]
    issue = lambda d: DeviceIssue(device_id=d.device_id, name=d.name, region=d.region,  # noqa: E731
                                  longest_silence_min=d.longest_silence_min,
                                  silences_over_threshold=d.silences_over_threshold)
    return DataQuality(
        offline_threshold_min=report.offline_threshold_min,
        devices_checked=len(devices),
        no_readings=[issue(d) for d in devices if d.total_readings == 0],
        long_silences=[issue(d) for d in devices if d.silences_over_threshold],
    )


def _manual_vs_ai(session: Session, ids: list[int], start: datetime, stop: datetime) -> ManualVsAi:
    rows = session.query(ManualTrapCount, Device).join(Device, Device.id == ManualTrapCount.device_id).filter(
        ManualTrapCount.device_id.in_(ids), ManualTrapCount.period_start >= start,
        ManualTrapCount.period_end <= stop,
    ).order_by(ManualTrapCount.period_end, ManualTrapCount.id).all() if ids else []
    collections = compare_collections(session, rows)
    # Same rule as /model-performance: a device that sent nothing has no AI count to compare.
    compared = [c for c in collections if c.device_reporting]
    groups = [g for c in compared for g in c.groups]
    return ManualVsAi(
        collections=len(collections), collections_compared=len(compared),
        collections_not_reporting=len(collections) - len(compared),
        manual_total=sum(g.manual for g in groups), ai_total=sum(g.ai for g in groups),
        abs_difference=sum(abs(g.difference) for g in groups),
        rule=COLLECTION_RULE,
        message=None if collections else "No manual trap counts this period",
    )


# ── Route ────────────────────────────────────────────────────────────────────

@router.get("/surveillance", response_model=SurveillanceReport)
def surveillance_report(
    period: Period = Query(default="weekly"),
    end: Optional[date] = Query(default=None, description="Last day of a weekly report / any day of the month (UTC). Default today."),
    cluster_id: Optional[int] = Query(default=None),
    region: Optional[str] = Query(default=None, description="Exact region name (case-insensitive)."),
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(require_member),
):
    now = datetime.utcnow()
    end = end or now.date()
    if end > now.date():
        raise HTTPException(status_code=422, detail="end cannot be in the future")
    allowed, out_of_scope = effective_cluster_scope(session, current_user, cluster_id)
    if out_of_scope:
        raise HTTPException(status_code=404, detail="Cluster not found")

    device_q = session.query(Device)
    if allowed is not None:
        device_q = device_q.filter(Device.cluster_id.in_(allowed))
    region = region.strip() if region else None
    if region:
        device_q = device_q.filter(func.lower(Device.region) == region.lower())
    start, stop = period_bounds(period, end)
    prev_start, prev_stop = previous_bounds(period, start)
    last = stop - timedelta(microseconds=1)  # DashboardService windows are inclusive
    complete = stop <= now

    # Data from any scoped device counts, but a device registered after the
    # period ended can't be "in scope" or "silent" for it. created_at is local
    # datetime.now (model default), compared as-is with naive UTC bounds.
    known = {d.id: d for d in device_q.all()}
    ids = list(known)
    devices = [d for d in known.values() if d.created_at is None or d.created_at < stop]
    mgmt = report_scope(current_user)
    mgmt_devices = [d for d in devices if mgmt is None or d.cluster_id in mgmt]

    current = detection_stats(session, ids, start, stop)
    previous = detection_stats(session, ids, prev_start, prev_stop)
    change = current["mosquito_total"] - previous["mosquito_total"] if complete else None
    reporting = _reporting_ids(session, ids, start, stop)

    dash = DashboardService(session)
    breakdown = dash._compute_breakdown(ids, start, last, "custom")
    by_sex: dict = defaultdict(int)
    for item in breakdown.sex:  # "female Aedes" → female
        by_sex[item.name.split(" ", 1)[0]] += item.count
    classified = sum(i.count for i in breakdown.genus)

    if allowed is None:
        scope = "All clusters"
    else:
        names = sorted(n for (n,) in session.query(DeviceCluster.name).filter(DeviceCluster.id.in_(allowed)))
        scope = "Clusters: " + (", ".join(names) or "none")
    if region:
        scope += f" · Region: {region}" if devices else f" · No devices in region '{region}' within your scope"
    management_scope = ("All clusters" if mgmt is None
                        else "Your own cluster only (public clusters excluded)")

    return SurveillanceReport(
        generated_at=now, period=period, period_start=start, period_end=stop,
        period_complete=complete, previous_start=prev_start, previous_end=prev_stop,
        scope=scope, management_scope=management_scope, cluster_id=cluster_id, region=region,
        summary=Summary(
            detections=current["mosquito_total"], detection_events=current["mosquito_events"],
            no_device_reported=not reporting,
            previous_detections=previous["mosquito_total"],
            previous_devices_reporting=len(_reporting_ids(session, ids, prev_start, prev_stop)),
            change=change,
            change_pct=(round(100 * change / previous["mosquito_total"], 1)
                        if change is not None and previous["mosquito_total"] else None),
            top_species=current["top_species"],
            devices_reporting=len(reporting), devices_total=len(devices),
            regions_covered=sorted({known[i].region for i in reporting if known[i].region}),
        ),
        detections=Detections(
            by_genus=breakdown.genus,
            by_sex=sorted((BreakdownItem(name=k, count=v) for k, v in by_sex.items()), key=lambda i: -i.count),
            by_species=breakdown.species,
            by_region=dash._compute_region_chart(ids, start, last, "custom").data,
            # Days that haven't started yet are not measured — left out.
            daily=_daily(session, ids, start, min(stop, datetime.combine(now.date() + timedelta(days=1), time()))),
            unclassified=current["mosquito_total"] - classified,
        ),
        environment=_environment(session, ids, start, stop),
        alerts=_alerts(session, [d.id for d in mgmt_devices], start, stop),
        data_quality=_data_quality(session, mgmt, [d.id for d in mgmt_devices], start, stop, now),
        manual_vs_ai=_manual_vs_ai(session, ids, start, stop),
    )
