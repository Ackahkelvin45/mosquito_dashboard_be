from fastapi import APIRouter, Depends, status
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session
from app.device.schema import MosquitoEventResponse

from app.core.database import get_db
from app.service.device_service import DeviceService




security = HTTPBearer()

router = APIRouter(tags=["mosquito"])


@router.get("", status_code=status.HTTP_200_OK, response_model=list[MosquitoEventResponse], dependencies=[Depends(security)])
def get_all_mosquito_events(session: Session = Depends(get_db)):
    try:
        return DeviceService(session).get_all_mosquito_events()
    except Exception as e:
        raise e
  
