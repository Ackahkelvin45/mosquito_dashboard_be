"""Manual trap counts + comparison against the device's AI detections.

Write rules: any non-guest user adds counts for devices in their own cluster
(super admin: any device). Edit/delete: cluster ADMIN for their own cluster,
SUPER_ADMIN for all. Reads are scoped by visible_cluster_ids like every other
read. Out-of-scope devices are a 404 (not revealed); visible-but-not-yours
(public cluster) is a 403.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import AuditAction
from app.audit.recorder import audit
from app.authentication.enums import UserRole
from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.core.security.permissions import (
    assert_device_visible, is_super_admin, require_admin, require_roles, visible_cluster_ids,
)
from app.device.models import (
    LIVE_MOSQUITO_EVENT, LIVE_SENSOR_READING, Device, MosquitoEvent,
    MosquitoIndividualReading, SensorDeviceReading,
)
from app.trap_counts.models import GENERA, SEXES, ManualTrapCount
from utils.protected_route import get_current_user

router = APIRouter()

require_member = require_roles(UserRole.USER, UserRole.ADMIN, UserRole.SUPER_ADMIN)

# A field worker's watch vs the server clock: "emptied just now" must not be
# rejected as future. Tune if devices/phones drift more.
FUTURE_TOLERANCE = timedelta(minutes=5)

Genus = Literal["anopheles", "aedes", "culex", "other"]
Sex = Literal["female", "male", "unknown"]


# ── Schemas ──────────────────────────────────────────────────────────────────

class ManualCountEntry(BaseModel):
    genus: Genus
    sex: Sex
    count: int = Field(ge=0)
    notes: Optional[str] = Field(default=None, max_length=500)


class ManualCountCreate(BaseModel):
    device_id: int
    period_start: datetime
    period_end: datetime
    entries: list[ManualCountEntry] = Field(min_length=1)

    @field_validator("period_start", "period_end")
    @classmethod
    def _naive_utc(cls, value: datetime) -> datetime:
        """The DB stores naive UTC; an aware input is converted, a naive one is taken as UTC."""
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value


class ManualCountUpdate(BaseModel):
    count: Optional[int] = Field(default=None, ge=0)
    notes: Optional[str] = Field(default=None, max_length=500)


class ManualCountResponse(BaseModel):
    id: int
    device_id: int
    period_start: datetime
    period_end: datetime
    genus: str
    sex: str
    count: int
    notes: Optional[str] = None
    entered_by_user_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ComparisonGroup(BaseModel):
    genus: str
    sex: str
    manual: int
    ai: int
    difference: int  # ai - manual


class AiOnlyGroup(BaseModel):
    genus: str
    sex: str
    ai: int


class CollectionComparison(BaseModel):
    device_id: int
    device_name: str
    cluster_id: Optional[int] = None
    period_start: datetime
    period_end: datetime
    # False = no live sensor reading in the period: an AI count of 0 then
    # means "device not reporting", not "no mosquitoes".
    device_reporting: bool
    entries: list[ManualCountResponse]
    # Only the genus x sex groups staff actually entered; totals cover these.
    groups: list[ComparisonGroup]
    manual_total: int
    ai_total: int
    difference: int  # ai_total - manual_total
    # AI detections in groups nobody counted by hand — shown, never compared.
    ai_not_counted: list[AiOnlyGroup]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ai_group(genus: Optional[str], sex: Optional[str]) -> tuple[str, str]:
    """Map stored AI strings onto the manual genus x sex buckets. Genus is
    stored lowercase; sex is "" when missing. Anything unrecognised (or an
    event without its reading row) lands in other/unknown."""
    g = (genus or "").strip().lower()
    s = (sex or "").strip().lower()
    return (g if g in GENERA else "other", s if s in SEXES else "unknown")


def _ai_counts(session: Session, device_id: int, start: datetime, end: datetime) -> dict:
    rows = (
        session.query(MosquitoIndividualReading.genus, MosquitoIndividualReading.sex,
                      func.count(MosquitoEvent.id))
        .outerjoin(MosquitoIndividualReading, MosquitoIndividualReading.batch_id == MosquitoEvent.id)
        .filter(MosquitoEvent.device_id == device_id,
                MosquitoEvent.timestamp >= start, MosquitoEvent.timestamp < end,
                LIVE_MOSQUITO_EVENT)
        .group_by(MosquitoIndividualReading.genus, MosquitoIndividualReading.sex)
        .all()
    )
    counts: dict = defaultdict(int)
    for genus, sex, n in rows:
        counts[_ai_group(genus, sex)] += n
    return counts


def _device_reporting(session: Session, device_id: int, start: datetime, end: datetime) -> bool:
    return session.query(
        session.query(SensorDeviceReading.id)
        .filter(SensorDeviceReading.device_id == device_id,
                SensorDeviceReading.timestamp >= start, SensorDeviceReading.timestamp < end,
                LIVE_SENSOR_READING)
        .exists()
    ).scalar()


def _load_for_edit(session: Session, user: UserResponse, count_id: int) -> ManualTrapCount:
    row = session.get(ManualTrapCount, count_id)
    device = session.get(Device, row.device_id) if row else None
    if row is None:
        raise HTTPException(status_code=404, detail="Trap count not found")
    assert_device_visible(session, user, device)
    if not is_super_admin(user) and device.cluster_id != user.cluster_id:
        raise HTTPException(status_code=403, detail="You can only edit trap counts in your own cluster")
    return row


def compare_collections(session: Session, rows: list) -> list[CollectionComparison]:
    """Group (ManualTrapCount, Device) rows into collections and compare each
    with the live AI detections over the same half-open period. Keeps the
    order of `rows`. Shared with /model-performance."""
    collections: dict = {}
    for row, device in rows:
        key = (row.device_id, row.period_start, row.period_end)
        collections.setdefault(key, (device, []))[1].append(row)

    # ponytail: two queries per collection; batch into one GROUP BY if the
    # number of collections ever makes this page slow.
    result = []
    for (dev_id, start, end), (device, entries) in collections.items():
        manual: dict = defaultdict(int)
        for e in entries:
            manual[(e.genus, e.sex)] += e.count
        ai = _ai_counts(session, dev_id, start, end)
        # Compare only what was entered: a group nobody counted is unknown, not 0.
        keys = [(g, s) for g in GENERA for s in SEXES]
        groups = [
            ComparisonGroup(genus=g, sex=s, manual=manual[(g, s)], ai=ai.get((g, s), 0),
                            difference=ai.get((g, s), 0) - manual[(g, s)])
            for g, s in keys if (g, s) in manual
        ]
        manual_total, ai_total = sum(manual.values()), sum(g.ai for g in groups)
        result.append(CollectionComparison(
            device_id=dev_id, device_name=device.name, cluster_id=device.cluster_id,
            period_start=start, period_end=end,
            device_reporting=_device_reporting(session, dev_id, start, end),
            entries=[ManualCountResponse.model_validate(e) for e in entries],
            groups=groups, manual_total=manual_total, ai_total=ai_total,
            difference=ai_total - manual_total,
            ai_not_counted=[AiOnlyGroup(genus=g, sex=s, ai=ai[(g, s)])
                            for g, s in keys if (g, s) not in manual and ai.get((g, s))],
        ))
    return result


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("", response_model=list[CollectionComparison])
def list_collections(
    device_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Every manual collection in scope, newest first, each compared to the AI."""
    q = session.query(ManualTrapCount, Device).join(Device, Device.id == ManualTrapCount.device_id)
    visible = visible_cluster_ids(session, current_user)
    if visible is not None:
        q = q.filter(Device.cluster_id.in_(visible))
    if device_id is not None:
        q = q.filter(ManualTrapCount.device_id == device_id)
    rows = q.order_by(ManualTrapCount.period_end.desc(), ManualTrapCount.id).all()
    return compare_collections(session, rows)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=list[ManualCountResponse])
def create_counts(
    payload: ManualCountCreate,
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(require_member),
):
    device = session.get(Device, payload.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    assert_device_visible(session, current_user, device)
    if not is_super_admin(current_user) and device.cluster_id != current_user.cluster_id:
        raise HTTPException(status_code=403, detail="You can only add trap counts for devices in your own cluster")
    if payload.period_end <= payload.period_start:
        raise HTTPException(status_code=422, detail="period_end must be after period_start")
    if payload.period_end > datetime.utcnow() + FUTURE_TOLERANCE:
        raise HTTPException(status_code=422, detail="period_end cannot be in the future")
    groups = [(e.genus, e.sex) for e in payload.entries]
    if len(set(groups)) != len(groups):
        raise HTTPException(status_code=422, detail="Each genus/sex combination may appear only once")
    # Periods are half-open [start, end): back-to-back collections don't overlap.
    # The exact same period is the same collection (adding more groups).
    overlap = (
        session.query(ManualTrapCount)
        .filter(ManualTrapCount.device_id == device.id,
                ManualTrapCount.period_start < payload.period_end,
                ManualTrapCount.period_end > payload.period_start,
                ~and_(ManualTrapCount.period_start == payload.period_start,
                      ManualTrapCount.period_end == payload.period_end))
        .first()
    )
    if overlap:
        raise HTTPException(status_code=409, detail=(
            f"This period overlaps an existing collection from {overlap.period_start.isoformat()} "
            f"to {overlap.period_end.isoformat()} UTC for this device"))

    rows = [
        ManualTrapCount(device_id=device.id, period_start=payload.period_start,
                        period_end=payload.period_end, genus=e.genus, sex=e.sex,
                        count=e.count, notes=e.notes, entered_by_user_id=current_user.id)
        for e in payload.entries
    ]
    session.add_all(rows)
    try:
        session.commit()
    except IntegrityError:  # uq_manual_trap_counts_collection_group
        session.rollback()
        raise HTTPException(status_code=409, detail="A count for this genus/sex already exists for this collection; edit it instead")
    for row in rows:
        session.refresh(row)
        audit(session, AuditAction.TRAP_COUNT_CREATED, actor_user_id=current_user.id,
              actor_email=current_user.email, target_type="manual_trap_count", target_id=row.id,
              detail={"device_id": row.device_id, "period_start": row.period_start.isoformat(),
                      "period_end": row.period_end.isoformat(), "genus": row.genus,
                      "sex": row.sex, "count": row.count})
    return rows


@router.patch("/{count_id}", response_model=ManualCountResponse)
def update_count(
    count_id: int,
    payload: ManualCountUpdate,
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(require_admin),
):
    row = _load_for_edit(session, current_user, count_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("count", 0) is None:
        raise HTTPException(status_code=422, detail="count cannot be null")
    before = {k: getattr(row, k) for k in changes}
    for k, v in changes.items():
        setattr(row, k, v)
    session.commit()
    session.refresh(row)
    audit(session, AuditAction.TRAP_COUNT_UPDATED, actor_user_id=current_user.id,
          actor_email=current_user.email, target_type="manual_trap_count", target_id=row.id,
          detail={"before": before, "after": changes})
    return row


@router.delete("/{count_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_count(
    count_id: int,
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(require_admin),
):
    row = _load_for_edit(session, current_user, count_id)
    detail = {"device_id": row.device_id, "period_start": row.period_start.isoformat(),
              "period_end": row.period_end.isoformat(), "genus": row.genus,
              "sex": row.sex, "count": row.count}
    session.delete(row)
    session.commit()
    audit(session, AuditAction.TRAP_COUNT_DELETED, actor_user_id=current_user.id,
          actor_email=current_user.email, target_type="manual_trap_count", target_id=count_id,
          detail=detail)
