from fastapi import APIRouter, Depends, status, Query
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session
from app.device.schema import MosquitoEventResponse
from datetime import datetime, timezone
from typing import Optional, List

from app.core.database import get_db
from utils.protected_route import get_current_user_or_guest
from app.core.pagination import Page
from app.service.device_service import DeviceService
from utils.time_range import compute_datetime_range, TimeRange
from app.authentication.schema import UserResponse
from app.core.security.permissions import managed_cluster_ids, visible_cluster_ids




security = HTTPBearer()

router = APIRouter(tags=["mosquito"])


@router.get("/filter-options", status_code=status.HTTP_200_OK)
def get_mosquito_filter_options(
    session: Session = Depends(get_db),
    # Guests see filter values derived from public-cluster data only.
    current_user: UserResponse = Depends(get_current_user_or_guest),
):
    """Distinct regions, genus, species and sex values for the historical-data filter dropdowns."""
    try:
        allowed = visible_cluster_ids(session, current_user)
        return DeviceService(session).get_mosquito_filter_options(allowed_cluster_ids=allowed)
    except Exception as e:
        raise e


@router.get("", status_code=status.HTTP_200_OK, response_model=Page[MosquitoEventResponse])
def get_all_mosquito_events(
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    start_date: Optional[datetime] = Query(default=None),
    end_date: Optional[datetime] = Query(default=None),
    search: Optional[str] = Query(default=None),
    range_: Optional[TimeRange] = Query(default=None, alias="range"),
    at: Optional[datetime] = Query(default=None),
    region: Optional[List[str]] = Query(default=None, description="Repeatable: matches any of the given regions"),
    device_uuid: Optional[List[str]] = Query(default=None),
    genus: Optional[List[str]] = Query(default=None, description="Repeatable: matches any of the given genus values"),
    species: Optional[List[str]] = Query(default=None, description="Repeatable: matches any of the given species values"),
    sex: Optional[List[str]] = Query(default=None, description="Repeatable: exact, case-insensitive match on any of the given sex values"),
    min_confidence: Optional[float] = Query(default=None, ge=0, le=1, description="Only detections with p_mosq >= this (0-1); detections with unknown (null) confidence are excluded"),
    cluster_id: Optional[List[int]] = Query(default=None, description="Repeatable: matches any of the given clusters (super admin only — intersected with your own scope otherwise)"),
    current_user: UserResponse = Depends(get_current_user_or_guest),
):
    try:
        if start_date is None and end_date is None and range_:
            window_at = at or datetime.now(timezone.utc)
            start_date, end_date = compute_datetime_range(range_, window_at)
        # Historical data is scoped to the caller's visible clusters.
        allowed = visible_cluster_ids(session, current_user)
        return DeviceService(session).get_all_mosquito_events(
            page=page,
            page_size=page_size,
            start_date=start_date,
            end_date=end_date,
            search=search,
            region=region,
            device_uuids=device_uuid,
            genus=genus,
            species=species,
            sex=sex,
            min_confidence=min_confidence,
            cluster_id=cluster_id,
            allowed_cluster_ids=allowed,
            # Excluder names only for clusters the viewer manages.
            managed_cluster_ids=managed_cluster_ids(current_user),
        )
    except Exception as e:
        raise e
  
