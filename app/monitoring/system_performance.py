"""System performance — every measure in the proposal, on one list.

Each measure says whether it is measured, measurable but empty in the period
("no_data"), or not measured at all ("not_measured", value null, with what
would be needed). Nothing is estimated or stood in by a proxy.

Device measures (uptime, completeness, GPS fix) are management figures:
ADMIN and SUPER_ADMIN only, scoped like data_quality.report_scope (own cluster,
super admin all), so public clusters' devices are never shown to outsiders.
Accuracy per site is the Agreement section's own collections grouped by site,
so it has that section's scope. Shown inside /model-performance.
"""
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel
from sqlalchemy import and_, case, extract, func, or_
from sqlalchemy.orm import Session

from app.device.models import DEVICE_SENT_SENSOR_READING, LIVE_SENSOR_READING, Device, SensorDeviceReading as R
from app.device.schema import OFFLINE_AFTER_MIN
from app.monitoring.data_quality import CLOCK_AHEAD_TOLERANCE, _count_if
from app.monitoring.device_health import gps_fix

if TYPE_CHECKING:
    from app.monitoring.model_performance import Target
    from app.trap_counts.routes import CollectionComparison

Status = Literal["measured", "not_measured", "no_data"]


class MeasureRow(BaseModel):
    label: str                    # device name or site
    value: Optional[float] = None
    detail: Optional[str] = None


class Measure(BaseModel):
    key: str
    name: str
    value: Optional[float] = None
    unit: Optional[str] = None
    status: Status
    period: str
    how: str                      # how it is measured, or why it is not
    rows: list[MeasureRow] = []   # per device / per site


def _pct(part: float, whole: float) -> float:
    return round(100 * part / whole, 1)


def _not_measured(key: str, name: str, unit: Optional[str], period: str, why: str) -> Measure:
    return Measure(key=key, name=name, unit=unit, status="not_measured", period=period, how=why)


def uptime(start: datetime, end: datetime, prev: Optional[datetime], first: Optional[datetime],
           last: Optional[datetime], inner_down_s: float) -> Optional[float]:
    """% of the period covered by data. A gap between readings counts as down
    only after the offline threshold has passed (gap − threshold), the same
    rule as the Offline badge: the gap into the period (from `prev`, the
    reading before it), the gaps inside it (`inner_down_s`, already reduced by
    SQL) and the ongoing one, all cut to the period. With no reading before
    the period, it is measured from the device's first reading in it."""
    t = timedelta(minutes=OFFLINE_AFTER_MIN)
    if first is None:  # nothing in the period
        if prev is None:
            return None
        down, length = end - max(start, prev + t), end - start
    else:
        first, last = min(first, end), min(last, end)  # clock-ahead readings
        length = end - (start if prev else first)
        if length.total_seconds() <= 0:
            return None
        down = timedelta(seconds=inner_down_s) + max(end - last - t, timedelta(0))
        if prev:
            down += max(first - max(start, prev + t), timedelta(0))
    return round(100 * max(0.0, 1 - max(down, timedelta(0)) / length), 1)


def _device_figures(session: Session, devices: list[Device], start: datetime, end: datetime):
    ids = [d.id for d in devices]
    sent = (R.device_id.in_(ids), LIVE_SENSOR_READING, DEVICE_SENT_SENSOR_READING)
    in_period = (*sent, R.timestamp >= start, R.timestamp <= end + CLOCK_AHEAD_TOLERANCE)

    ts = extract("epoch", R.timestamp)
    lagged = (
        session.query(R.device_id.label("device_id"), R.timestamp.label("ts"),
                      (ts - func.lag(ts).over(partition_by=R.device_id,
                                              order_by=(R.timestamp, R.id))).label("gap"))
        .filter(*in_period).subquery()
    )
    threshold = OFFLINE_AFTER_MIN * 60
    spans = {
        row[0]: row[1:] for row in session.query(
            lagged.c.device_id, func.min(lagged.c.ts), func.max(lagged.c.ts),
            func.coalesce(func.sum(case((lagged.c.gap > threshold, lagged.c.gap - threshold), else_=0)), 0),
        ).group_by(lagged.c.device_id).all()
    }
    prev = dict(session.query(R.device_id, func.max(R.timestamp))
                .filter(*sent, R.timestamp < start).group_by(R.device_id).all())
    present = {
        row[0]: row[1:] for row in session.query(
            R.device_id, func.count(R.id),
            func.count(func.coalesce(R.external_temperature, R.internal_temperature)),
            func.count(func.coalesce(R.external_humidity, R.internal_humidity)),
            # An impossible battery_pct is not a reading (same rule as device health).
            _count_if(or_(and_(R.battery_pct >= 0, R.battery_pct <= 100), R.battery_voltage.isnot(None))),
        ).filter(*in_period).group_by(R.device_id).all()
    }
    return spans, prev, present


def _accuracy_per_site(collections: "list[CollectionComparison]", site_of: dict[int, str],
                       period: str) -> Measure:
    sites: dict = {}  # site -> [collections, groups, exact, manual, ai, Σ|diff|]
    for c in collections:
        if not c.device_reporting:  # its AI 0 means "not reporting", not a count
            continue
        s = sites.setdefault(site_of[c.device_id], [0, 0, 0, 0, 0, 0])
        s[0] += 1
        for g in c.groups:
            s[1] += 1
            s[2] += g.difference == 0
            s[3] += g.manual
            s[4] += g.ai
            s[5] += abs(g.difference)
    groups = sum(s[1] for s in sites.values())
    how = ("Agreement of COUNTS, not per-detection accuracy: the share of hand-counted genus/sex groups "
           "where the device's count equals the manual trap count over the same collection period. "
           "Same data as the Agreement with manual trap counts section, grouped by site (region / "
           "community); collections whose device sent no data are left out.")
    return Measure(
        key="accuracy_per_site", name="Accuracy per site (manual vs AI count agreement)",
        value=_pct(sum(s[2] for s in sites.values()), groups) if groups else None, unit="%",
        status="measured" if groups else "no_data", period=period,
        how=how if groups else how + " No manual trap counts from reporting devices in this period.",
        rows=[MeasureRow(label=site, value=_pct(ex, n) if n else None,
                         detail=f"{cols} collection(s): manual {m}, AI {a}, total difference {d}")
              for site, (cols, n, ex, m, a, d) in sorted(sites.items())],
    )


def _device_measures(session: Session, scope: Optional[set[int]], start: datetime, end: datetime,
                     period: str) -> list[Measure]:
    q = session.query(Device)
    if scope is not None:
        q = q.filter(Device.cluster_id.in_(scope))
    devices = q.order_by(Device.name, Device.id).all()
    spans, prev, present = _device_figures(session, devices, start, end) if devices else ({}, {}, {})

    up_rows, comp_rows, gps_rows = [], [], []
    comp_total = comp_present = 0
    for d in devices:
        first, last, inner = spans.get(d.id, (None, None, 0))
        u = uptime(start, end, prev.get(d.id), first, last, float(inner))
        up_rows.append(MeasureRow(label=d.name, value=u,
                                  detail=None if u is not None else "Has never sent sensor data"))
        total, *counts = present.get(d.id, (0, 0, 0, 0))
        comp_total += 3 * total
        comp_present += sum(counts)
        comp_rows.append(MeasureRow(label=d.name, value=_pct(sum(counts), 3 * total) if total else None,
                                    detail=f"{total} reading(s)" if total else "No readings in this period"))
        fix = gps_fix(d)
        gps_rows.append(MeasureRow(label=d.name, value=None if fix is None else float(fix),
                                   detail={True: "Usable fix", False: "Position stored but not a usable fix (e.g. 0,0)",
                                           None: "No position stored"}[fix]))

    up_values = [r.value for r in up_rows if r.value is not None]
    with_fix = sum(r.value == 1 for r in gps_rows)
    return [
        Measure(key="uptime", name="Uptime", unit="%", period=period,
                value=round(sum(up_values) / len(up_values), 1) if up_values else None,
                status="measured" if up_values else "no_data",
                how=(f"Share of the period covered by live sensor data the device sent itself (readings an admin "
                     f"excluded from analysis still count: the device did send them). A gap between readings "
                     f"counts as down only after {OFFLINE_AFTER_MIN} minutes, when the device would show as "
                     "Offline; that includes a gap still going on. Measured from the device's first reading in "
                     "the period if it had none before. Fleet figure = average of devices that have reported."),
                rows=up_rows),
        Measure(key="data_completeness", name="Value completeness (of readings received)", unit="%", period=period,
                value=_pct(comp_present, comp_total) if comp_total else None,
                status="measured" if comp_total else "no_data",
                how=("Share of core values present in live readings the device sent: temperature and humidity "
                     "(present if the outside OR inside sensor gave a value) and battery (a 0–100 percentage or a "
                     "voltage). Readings that never arrived are not counted here; that is uptime. "
                     "Fleet figure = all values present / all values expected."),
                rows=comp_rows),
        Measure(key="gps_fix", name="Devices with a GPS fix", value=with_fix if devices else None,
                unit=f"of {len(devices)} devices", period="Current stored position",
                status="measured" if devices else "no_data",
                how="A device counts when its stored position is a usable fix (not missing, not 0,0 or out of range).",
                rows=gps_rows),
    ]


def build_measures(session: Session, device_scope: Optional[set[int]], device_measures: bool,
                   start: datetime, end: datetime, days: int, targets: "list[Target]",
                   collections: "list[CollectionComparison]", site_of: dict[int, str]) -> list[Measure]:
    """`device_measures` False (USER role) leaves out uptime, completeness and GPS fix."""
    period = f"Last {days} days"
    zone_why = ("Needs detections checked one by one against confirmed specimens; manual counts only compare "
                "totals. Devices and regions also have no ecological-zone field yet.")
    measures = _device_measures(session, device_scope, start, end, period) if device_measures else []
    measures += [
        _not_measured("gps_accuracy", "GPS accuracy", "m", period,
                      "Not measured: devices don't send fix accuracy (HDOP)."),
        _not_measured("audio_quality", "Audio quality", None, period,
                      "Not measured: devices don't send audio quality metrics."),
        _accuracy_per_site(collections, site_of, period),
        _not_measured("false_positives_by_zone", "False positives per ecological zone", "%", period, zone_why),
        _not_measured("false_negatives_by_zone", "False negatives per ecological zone", "%", period, zone_why),
        _not_measured("zone_bias", "Bias check between ecological zones", None, period, zone_why),
        _not_measured("load_time", "Page load time", "s", period,
                      "Not measured: page load time is not recorded yet (it would need the dashboard to log timings)."),
    ]
    measures += [_not_measured(f"target_{t.name}", f"{t.name.capitalize()} accuracy (target above {t.target_pct}%)",
                               "%", "Proposal target", t.reason) for t in targets]
    return measures
