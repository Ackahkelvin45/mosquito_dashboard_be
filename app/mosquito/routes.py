from fastapi import APIRouter, Depends, status, Query
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session
from app.device.schema import MosquitoEventResponse
from datetime import datetime, timezone
from typing import Optional, List

from app.core.database import get_db
from app.core.pagination import Page
from app.service.device_service import DeviceService
from utils.time_range import compute_datetime_range, TimeRange




security = HTTPBearer()

router = APIRouter(tags=["mosquito"])


@router.get("", status_code=status.HTTP_200_OK, response_model=Page[MosquitoEventResponse], dependencies=[Depends(security)])
def get_all_mosquito_events(
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    start_date: Optional[datetime] = Query(default=None),
    end_date: Optional[datetime] = Query(default=None),
    search: Optional[str] = Query(default=None),
    range_: Optional[TimeRange] = Query(default=None, alias="range"),
    at: Optional[datetime] = Query(default=None),
    region: Optional[str] = Query(default=None),
    device_uuid: Optional[List[str]] = Query(default=None),
    genus: Optional[str] = Query(default=None),
    species: Optional[str] = Query(default=None),
):
    try:
        if start_date is None and end_date is None and range_:
            window_at = at or datetime.now(timezone.utc)
            start_date, end_date = compute_datetime_range(range_, window_at)
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
        )
    except Exception as e:
        raise e
  
