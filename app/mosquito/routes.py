from fastapi import APIRouter, Depends, status, Query
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session
from app.device.schema import MosquitoEventResponse
from datetime import datetime, timezone
from typing import Optional

from app.core.database import get_db
from app.service.device_service import DeviceService
from utils.time_range import compute_datetime_range, TimeRange




security = HTTPBearer()

router = APIRouter(tags=["mosquito"])


@router.get("", status_code=status.HTTP_200_OK, response_model=list[MosquitoEventResponse], dependencies=[Depends(security)])
def get_all_mosquito_events(
    session: Session = Depends(get_db),
    start_date: Optional[datetime] = Query(default=None),
    end_date: Optional[datetime] = Query(default=None),
    search: Optional[str] = Query(default=None),
    range_: Optional[TimeRange] = Query(default=None, alias="range"),
    at: Optional[datetime] = Query(default=None),
):
    try:
        if start_date is None and end_date is None and range_:
            window_at = at or datetime.now(timezone.utc)
            start_date, end_date = compute_datetime_range(range_, window_at)
        return DeviceService(session).get_all_mosquito_events(
            start_date=start_date,
            end_date=end_date,
            search=search,
        )
    except Exception as e:
        raise e
  
