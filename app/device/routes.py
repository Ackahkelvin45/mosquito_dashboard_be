from fastapi import APIRouter,Depends,Query,BackgroundTasks
from fastapi.security import HTTPBearer
from app.device.models import Device
from app.device.schema import DeviceCreate, DeviceResponse,DeviceUpdate,DeviceClusterCreate, DeviceClusterResponse, DeviceClusterUpdate
from app.core.database import get_db
from sqlalchemy.orm import Session
from utils.protected_route import get_current_user
from fastapi.security import HTTPBearer
from app.service.device_service import DeviceService
from app.service.device_cluster_service import DeviceClusterService
from fastapi import status
from datetime import datetime
from typing import Optional
from app.service.email_service import send_researcher_request_email





security = HTTPBearer()


router = APIRouter(
    tags=["devices"],
)



@router.post("",status_code=status.HTTP_201_CREATED,response_model=DeviceResponse,dependencies=[Depends(security)])
def create_device(device_data: DeviceCreate, session: Session = Depends(get_db)):
    try:
        device_service = DeviceService(session)
        return device_service.create_device(device_data)
    except Exception as e:
        raise e
    

@router.get("",status_code=status.HTTP_200_OK,response_model=list[DeviceResponse],dependencies=[Depends(security)])
def get_devices(session: Session = Depends(get_db),
                name: Optional[str] = Query(default=None),
                region: Optional[str] = Query(default=None),
                max_mosquito_count: Optional[int] = Query(default=None),
                min_mosquito_count: Optional[int] = Query(default=None),
                created_after: Optional[datetime] = Query(default=None),
                longitude: Optional[float] = Query(default=None),
                latitude: Optional[float] = Query(default=None),
                cluster_id: Optional[int] = Query(default=None)
                ):
    try:
        device_service = DeviceService(session)
        return device_service.get_devices(
            name=name,
            region=region,
            max_mosquito_count=max_mosquito_count,
            min_mosquito_count=min_mosquito_count,
            created_after=created_after,
            longitude=longitude,
            latitude=latitude
            ,cluster_id=cluster_id
        )
    except Exception as e:
        raise e
    


@router.get("/clusters", status_code=status.HTTP_200_OK, response_model=list[DeviceClusterResponse], dependencies=[Depends(security)])
def get_clusters(session: Session = Depends(get_db)):
    try:
        cluster_service = DeviceClusterService(session)
        return cluster_service.get_clusters()
    except Exception as e:
        raise e


@router.post("/clusters", status_code=status.HTTP_201_CREATED, response_model=DeviceClusterResponse, dependencies=[Depends(security)])
def create_cluster(cluster_data: DeviceClusterCreate, session: Session = Depends(get_db)):
    try:
        cluster_service = DeviceClusterService(session)
        return cluster_service.create_cluster(cluster_data)
    except Exception as e:
        raise e


@router.get("/clusters/{cluster_id}", status_code=status.HTTP_200_OK, response_model=DeviceClusterResponse, dependencies=[Depends(security)])
def get_cluster_by_id(cluster_id: int, session: Session = Depends(get_db)):
    try:
        cluster_service = DeviceClusterService(session)
        return cluster_service.get_cluster_by_id(cluster_id)
    except Exception as e:
        raise e


@router.patch("/clusters/{cluster_id}", status_code=status.HTTP_200_OK, response_model=DeviceClusterResponse, dependencies=[Depends(security)])
def update_cluster(cluster_id: int, cluster_data: DeviceClusterUpdate, session: Session = Depends(get_db)):
    try:
        cluster_service = DeviceClusterService(session)
        return cluster_service.update_cluster(cluster_id, cluster_data)
    except Exception as e:
        raise e


@router.delete("/clusters/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(security)])
def delete_cluster(cluster_id: int, session: Session = Depends(get_db)):
    try:
        cluster_service = DeviceClusterService(session)
        cluster_service.delete_cluster(cluster_id)
    except Exception as e:
        raise e


@router.get("/{device_id}", status_code=status.HTTP_200_OK, response_model=DeviceResponse, dependencies=[Depends(security)])
def get_device_by_id(device_id: int, session: Session = Depends(get_db)):
    try:
        device_service = DeviceService(session)
        return device_service.get_device_by_id(device_id)
    except Exception as e:
        raise e


@router.get("/uuid/{device_uuid}", status_code=status.HTTP_200_OK, response_model=DeviceResponse, dependencies=[Depends(security)])
def get_device_by_uuid(device_uuid: str, session: Session = Depends(get_db)):
    try:
        device_service = DeviceService(session)
        return device_service.get_device_by_uuid(device_uuid)
    except Exception as e:
        raise e


@router.patch("/{device_id}", status_code=status.HTTP_200_OK, response_model=DeviceResponse, dependencies=[Depends(security)])
def update_device(device_id: int, device_data: DeviceUpdate, session: Session = Depends(get_db)):
    try:
        device_service = DeviceService(session)
        return device_service.update_device(device_id, device_data)
    except Exception as e:
        raise e


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(security)])
def delete_device(device_id: int, session: Session = Depends(get_db)):
    try:
        device_service = DeviceService(session)
        device_service.delete_device(device_id)
    except Exception as e:
        raise e