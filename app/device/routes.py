from fastapi import APIRouter,Depends,BackgroundTasks
from fastapi.security import HTTPBearer
from app.device.models import Device
from app.device.schema import DeviceCreate, DeviceResponse,DeviceUpdate
from app.core.database import get_db
from sqlalchemy.orm import Session
from utils.protected_route import get_current_user
from fastapi.security import HTTPBearer

from app.service.device_service import DeviceService
from fastapi import status





security = HTTPBearer()


router = APIRouter(
    tags=["devices"],
)


security = HTTPBearer()
@router.post("/create",status_code=status.HTTP_201_CREATED,response_model=DeviceResponse,dependencies=[Depends(security)])
def create_device(device_data: DeviceCreate, session: Session = Depends(get_db)):
    try:
        device_service = DeviceService(session)
        return device_service.create_device(device_data)
    except Exception as e:
        raise e
    

@router.get("/",status_code=status.HTTP_200_OK,response_model=list[DeviceResponse],dependencies=[Depends(security)])
def get_devices(session: Session = Depends(get_db)):
    try:
        device_service = DeviceService(session)
        return device_service.get_devices()
    except Exception as e:
        raise e
    



@router.get("/{device_id}",status_code=status.HTTP_200_OK,response_model=DeviceResponse,dependencies=[Depends(security)])
def get_device_by_id(device_id: int, session: Session = Depends(get_db)):
    try:
        device_service = DeviceService(session)
        return device_service.get_device_by_id(device_id)
    except Exception as e:
        raise e
    


@router.get("/uuid/{device_uuid}",status_code=status.HTTP_200_OK,response_model=DeviceResponse,dependencies=[Depends(security)])
def get_device_by_uuid(device_uuid: str, session: Session = Depends(get_db)):
    try:
        device_service = DeviceService(session)
        return device_service.get_device_by_uuid(device_uuid)
    except Exception as e:
        raise e
    


@router.patch("/{device_id}",status_code=status.HTTP_200_OK,response_model=DeviceResponse,dependencies=[Depends(security)])
def update_device(device_id: int, device_data: DeviceUpdate, session: Session = Depends(get_db)):
    try:
        device_service = DeviceService(session)
        return device_service.update_device(device_id, device_data)
    except Exception as e:
        raise e

