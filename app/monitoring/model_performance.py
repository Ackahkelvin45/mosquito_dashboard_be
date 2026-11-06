"""Model performance — only what is actually measured, nothing inferred.

Two kinds of evidence, never mixed:
  * Confidence: what the device model reported per detection (p_mosq, the
    probability at the reported genus / sex key). This is the model's own
    certainty, NOT accuracy.
  * Count agreement: manual trap counts vs AI counts per counted genus x sex
    group, per collection (shared logic with /trap-counts). Manual counts can't
    say which individual detection was right, so this is count agreement, not
    per-detection accuracy. Differences are summed as |ai - manual| per group
    so over- and under-counts never cancel out.

The grant accuracy targets are echoed with status "not_measurable_yet": no
per-detection ground truth exists in the system. Scoped by visible_cluster_ids
(own cluster + public; super admin all). Live detections only.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.core.security.permissions import visible_cluster_ids
from app.device.models import ANALYSIS_MOSQUITO_EVENT, Device, MosquitoEvent, MosquitoIndividualReading
from app.monitoring.data_quality import _count_if
from app.trap_counts.models import ManualTrapCount
from app.trap_counts.routes import compare_collections, require_member

router = APIRouter()

LOW_CONFIDENCE = 0.5
# (label, lower inclusive, upper exclusive; None = open / up to 1 inclusive)
BUCKETS = (("<0.5", None, 0.5), ("0.5–0.7", 0.5, 0.7), ("0.7–0.9", 0.7, 0.9), ("≥0.9", 0.9, None))
TARGETS = (("detection", 80), ("sex", 75), ("age", 75))  # grant targets, % accuracy
COLLECTION_RULE = "A manual collection is included only if its whole period lies inside the window."


# ── Schemas ──────────────────────────────────────────────────────────────────

class Bucket(BaseModel):
    label: str
    count: int


class PMosqStats(BaseModel):
    measured: int          # detections with a 0–1 value
    missing: int           # absent / outside 0–1 — excluded, never 0
    mean: Optional[float] = None
    below_low: int         # measured values < LOW_CONFIDENCE
    below_low_share: Optional[float] = None
    buckets: list[Bucket]


class KeyConfidence(BaseModel):
    """Probability at the reported genus/sex key."""
    measured: int
    missing: int                # no reported key, no probs, key absent, or not 0–1
    zero_at_reported_key: int   # measured, but the value at the reported key is exactly 0
    mean: Optional[float] = None


class GenusRow(BaseModel):
    genus: str             # as reported, lowercased; "" → "unreported"
    detections: int
    genus_confidence: KeyConfidence


class InferenceStats(BaseModel):
    measured: int
    missing: int           # absent or negative
    mean_ms: Optional[float] = None
    max_ms: Optional[int] = None


class ConfidenceReport(BaseModel):
    detections: int
    p_mosq: PMosqStats
    genus_confidence: KeyConfidence
    sex_confidence: KeyConfidence
    binary_decision_true: int
    binary_decision_false: int
    binary_decision_missing: int
    inference_ms: InferenceStats
    age_group_reported: int  # detections with a non-empty age_group
    by_genus: list[GenusRow]


class GenusAgreement(BaseModel):
    genus: str
    groups: int
    manual: int
    ai: int
    abs_difference: int    # Σ |ai − manual| over this genus's groups


class CollectionRow(BaseModel):
    device_id: int
    device_name: str
    period_start: datetime
    period_end: datetime
    device_reporting: bool
    manual_total: int
    ai_total: int
    abs_difference: int


class AgreementReport(BaseModel):
    collection_rule: str
    # Collections whose device sent no live sensor data in the period: their
    # AI count of 0 means "not reporting", so they are listed but left out of
    # every figure below.
    collections: list[CollectionRow]
    collections_compared: int
    collections_not_reporting: int
    groups_compared: int        # counted genus x sex groups across compared collections
    groups_exact_match: int
    exact_match_share: Optional[float] = None
    manual_total: int
    ai_total: int
    abs_difference: int         # Σ |ai − manual| per group — errors never cancel
    by_genus: list[GenusAgreement]


class Target(BaseModel):
    name: str
    target_pct: int
    status: Literal["not_measurable_yet"]
    reason: str


class ModelPerformanceReport(BaseModel):
    days: int
    window_start: datetime
    window_end: datetime
    low_confidence_threshold: float
    confidence: ConfidenceReport
    agreement: AgreementReport
    targets: list[Target]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _at_key(probs, key: Optional[str]) -> Optional[float]:
    """The value at the reported key. Only that key carries the model's value;
    the others are 0 placeholders, so they are never read."""
    key = (key or "").strip().lower()
    if not key or not isinstance(probs, dict):
        return None
    value = {str(k).strip().lower(): v for k, v in probs.items()}.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return None
    return float(value)


def _key_stats(values: list[Optional[float]]) -> KeyConfidence:
    measured = [v for v in values if v is not None]
    return KeyConfidence(
        measured=len(measured), missing=len(values) - len(measured),
        zero_at_reported_key=sum(v == 0 for v in measured),
        mean=round(sum(measured) / len(measured), 4) if measured else None,
    )


def _round(value, digits: int) -> Optional[float]:
    return round(float(value), digits) if value is not None else None


def _confidence(session: Session, scope: Optional[set[int]], start: datetime, end: datetime) -> ConfidenceReport:
    R = MosquitoIndividualReading

    def base(*cols):
        q = (
            session.query(*cols)
            .select_from(MosquitoEvent)
            .join(Device, Device.id == MosquitoEvent.device_id)
            .outerjoin(R, R.batch_id == MosquitoEvent.id)  # no reading row → every value missing
            .filter(MosquitoEvent.timestamp >= start, MosquitoEvent.timestamp <= end, ANALYSIS_MOSQUITO_EVENT)
        )
        return q.filter(Device.cluster_id.in_(scope)) if scope is not None else q

    p = R.p_mosq
    p_ok = and_(p >= 0, p <= 1)
    ms_ok = R.inference_ms >= 0
    (total, p_n, p_mean, below, *bucket_counts, dec_true, dec_false,
     ms_n, ms_mean, ms_max, age_n) = base(
        func.count(MosquitoEvent.id),
        _count_if(p_ok),
        func.avg(case((p_ok, p))),
        _count_if(and_(p_ok, p < LOW_CONFIDENCE)),
        *[_count_if(and_(p_ok, *([p >= lo] if lo is not None else []), *([p < hi] if hi is not None else [])))
          for _, lo, hi in BUCKETS],
        _count_if(R.binary_decision.is_(True)),
        _count_if(R.binary_decision.is_(False)),
        _count_if(ms_ok),
        func.avg(case((ms_ok, R.inference_ms))),
        func.max(case((ms_ok, R.inference_ms))),
        _count_if(func.trim(func.coalesce(R.age_group, "")) != ""),
    ).one()

    # JSON keys are read in Python (portable across Postgres/SQLite); only the
    # needed columns, bounded by the window.
    genus_c, sex_c = [], []
    per_genus: dict = defaultdict(list)
    for genus, sex, taxon_probs, sex_probs in base(R.genus, R.sex, R.taxon_probs, R.sex_probs):
        gc = _at_key(taxon_probs, genus)
        genus_c.append(gc)
        sex_c.append(_at_key(sex_probs, sex))
        per_genus[(genus or "").strip().lower() or "unreported"].append(gc)

    return ConfidenceReport(
        detections=total,
        p_mosq=PMosqStats(
            measured=p_n, missing=total - p_n, mean=_round(p_mean, 4),
            below_low=below, below_low_share=round(below / p_n, 4) if p_n else None,
            buckets=[Bucket(label=label, count=n) for (label, _, _), n in zip(BUCKETS, bucket_counts)],
        ),
        genus_confidence=_key_stats(genus_c), sex_confidence=_key_stats(sex_c),
        binary_decision_true=dec_true, binary_decision_false=dec_false,
        binary_decision_missing=total - dec_true - dec_false,
        inference_ms=InferenceStats(measured=ms_n, missing=total - ms_n,
                                    mean_ms=_round(ms_mean, 1), max_ms=ms_max),
        age_group_reported=age_n,
        by_genus=sorted((GenusRow(genus=g, detections=len(v), genus_confidence=_key_stats(v))
                         for g, v in per_genus.items()), key=lambda row: -row.detections),
    )


def _agreement(session: Session, scope: Optional[set[int]], start: datetime, end: datetime) -> AgreementReport:
    q = (
        session.query(ManualTrapCount, Device)
        .join(Device, Device.id == ManualTrapCount.device_id)
        .filter(ManualTrapCount.period_start >= start, ManualTrapCount.period_end <= end)
    )
    if scope is not None:
        q = q.filter(Device.cluster_id.in_(scope))
    collections = compare_collections(
        session, q.order_by(ManualTrapCount.period_end.desc(), ManualTrapCount.id).all())

    compared = [c for c in collections if c.device_reporting]
    groups = [g for c in compared for g in c.groups]
    per_genus: dict = defaultdict(lambda: [0, 0, 0, 0])  # groups, manual, ai, Σ|diff|
    for g in groups:
        row = per_genus[g.genus]
        row[0] += 1
        row[1] += g.manual
        row[2] += g.ai
        row[3] += abs(g.difference)
    exact = sum(g.difference == 0 for g in groups)
    return AgreementReport(
        collection_rule=COLLECTION_RULE,
        collections=[CollectionRow(device_id=c.device_id, device_name=c.device_name,
                                   period_start=c.period_start, period_end=c.period_end,
                                   device_reporting=c.device_reporting,
                                   manual_total=c.manual_total, ai_total=c.ai_total,
                                   abs_difference=sum(abs(g.difference) for g in c.groups))
                     for c in collections],
        collections_compared=len(compared),
        collections_not_reporting=len(collections) - len(compared),
        groups_compared=len(groups), groups_exact_match=exact,
        exact_match_share=round(exact / len(groups), 4) if groups else None,
        manual_total=sum(g.manual for g in groups), ai_total=sum(g.ai for g in groups),
        abs_difference=sum(abs(g.difference) for g in groups),
        by_genus=[GenusAgreement(genus=g, groups=n, manual=m, ai=a, abs_difference=d)
                  for g, (n, m, a, d) in per_genus.items()],
    )


def _targets(age_group_reported: int) -> list[Target]:
    reasons = {
        "detection": "Needs each detection checked against a person's identification. Manual trap "
                     "counts only give count agreement, and mean confidence is not accuracy.",
        "sex": "Needs each detection's sex checked by a person. Manual counts by sex give count "
               "agreement only.",
        "age": ("No detection in this period has an age value (age_group is empty), so there is "
                "nothing to measure." if age_group_reported == 0 else
                f"{age_group_reported} detections report an age, but none is checked against a "
                "person's assessment."),
    }
    return [Target(name=n, target_pct=pct, status="not_measurable_yet", reason=reasons[n])
            for n, pct in TARGETS]


def build_report(session: Session, scope: Optional[set[int]], days: int,
                 now: Optional[datetime] = None) -> ModelPerformanceReport:
    window_end = now or datetime.utcnow()
    window_start = window_end - timedelta(days=days)
    confidence = _confidence(session, scope, window_start, window_end)
    return ModelPerformanceReport(
        days=days, window_start=window_start, window_end=window_end,
        low_confidence_threshold=LOW_CONFIDENCE,
        confidence=confidence,
        agreement=_agreement(session, scope, window_start, window_end),
        targets=_targets(confidence.age_group_reported),
    )


@router.get("/model-performance", response_model=ModelPerformanceReport)
def model_performance(
    days: int = Query(default=30, ge=1, le=365),
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(require_member),
):
    return build_report(session, visible_cluster_ids(session, current_user), days)
