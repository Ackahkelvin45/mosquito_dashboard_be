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
from app.core.security.permissions import visible_cluster_ids




security = HTTPBearer()

router = APIRouter(tags=["mosquito"])


@router.get("/filter-options", status_code=status.HTTP_200_OK)
def get_mosquito_filter_options(
    session: Session = Depends(get_db),
    # Guests see filter values derived from public-cluster data only.
    current_user: UserResponse = Depends(get_current_user_or_guest),
):
    """Distinct regions, genus and species values for the historical-data filter dropdowns."""
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
            allowed_cluster_ids=allowed,
        )
    except Exception as e:
        raise e
  
