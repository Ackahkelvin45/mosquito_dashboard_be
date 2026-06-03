from fastapi import APIRouter, Depends, Query, BackgroundTasks
from fastapi.security import HTTPBearer
from app.device.models import Device
from app.device.schema import (
    DeviceCreate, DeviceResponse, DeviceUpdate,
    DeviceClusterCreate, DeviceClusterResponse, DeviceClusterUpdate,
    MosquitoEventPayload, SensorDataPayload, SensorDataResponse,
    MosquitoIndividualPayload, MosquitoIndividualResponse, MosquitoEventResponse,
)
from app.device.chart_schema import DeviceChartsResponse
from app.core.database import get_db
from app.core.pagination import Page
from sqlalchemy.orm import Session
from utils.protected_route import get_current_user
from app.service.device_service import DeviceService
from app.service.device_cluster_service import DeviceClusterService
from app.service.device_chart_service import DeviceChartService
from app.service.email_service import send_researcher_request_email
from fastapi import status
from datetime import datetime, timezone
from typing import Optional, List
from utils.time_range import compute_datetime_range, TimeRange


security = HTTPBearer()

router = APIRouter(tags=["devices"])



@router.post("", status_code=status.HTTP_201_CREATED, response_model=DeviceResponse, dependencies=[Depends(security)])
def create_device(device_data: DeviceCreate, session: Session = Depends(get_db)):
    try:
        return DeviceService(session).create_device(device_data)
    except Exception as e:
        raise e


@router.get("", status_code=status.HTTP_200_OK, response_model=Page[DeviceResponse], dependencies=[Depends(security)])
def get_devices(
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    name: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
    device_uuid: Optional[str] = Query(default=None),
    max_mosquito_count: Optional[int] = Query(default=None),
    min_mosquito_count: Optional[int] = Query(default=None),
    created_after: Optional[datetime] = Query(default=None),
    longitude: Optional[float] = Query(default=None),
    latitude: Optional[float] = Query(default=None),
    cluster_id: Optional[int] = Query(default=None),
    trap_status: Optional[bool] = Query(default=None, description="Filter by device on/off status (latest reading's trap_status)"),
):
    try:
        return DeviceService(session).get_devices(
            page=page, page_size=page_size,
            name=name, region=region,
            device_uuid=device_uuid,
            max_mosquito_count=max_mosquito_count, min_mosquito_count=min_mosquito_count,
            created_after=created_after, longitude=longitude,
            latitude=latitude, cluster_id=cluster_id,
            trap_status=trap_status,
        )
    except Exception as e:
        raise e


@router.patch("/{device_id}", status_code=status.HTTP_200_OK, response_model=DeviceResponse, dependencies=[Depends(security)])
def update_device(device_id: int, device_data: DeviceUpdate, session: Session = Depends(get_db)):
    try:
        return DeviceService(session).update_device(device_id, device_data)
    except Exception as e:
        raise e
    

@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(security)])
def delete_device(device_id: int, session: Session = Depends(get_db)):
    try:
        DeviceService(session).delete_device(device_id)
    except Exception as e:
        raise e


@router.get("/clusters", status_code=status.HTTP_200_OK, response_model=Page[DeviceClusterResponse], dependencies=[Depends(security)])
def get_clusters(
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    try:
        return DeviceClusterService(session).get_clusters(page=page, page_size=page_size)
    except Exception as e:
        raise e


@router.post("/clusters", status_code=status.HTTP_201_CREATED, response_model=DeviceClusterResponse, dependencies=[Depends(security)])
def create_cluster(cluster_data: DeviceClusterCreate, session: Session = Depends(get_db)):
    try:
        return DeviceClusterService(session).create_cluster(cluster_data)
    except Exception as e:
        raise e


@router.get("/clusters/{cluster_id}", status_code=status.HTTP_200_OK, response_model=DeviceClusterResponse, dependencies=[Depends(security)])
def get_cluster_by_id(cluster_id: int, session: Session = Depends(get_db)):
    try:
        return DeviceClusterService(session).get_cluster_by_id(cluster_id)
    except Exception as e:
        raise e


@router.patch("/clusters/{cluster_id}", status_code=status.HTTP_200_OK, response_model=DeviceClusterResponse, dependencies=[Depends(security)])
def update_cluster(cluster_id: int, cluster_data: DeviceClusterUpdate, session: Session = Depends(get_db)):
    try:
        return DeviceClusterService(session).update_cluster(cluster_id, cluster_data)
    except Exception as e:
        raise e


@router.delete("/clusters/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(security)])
def delete_cluster(cluster_id: int, session: Session = Depends(get_db)):
    try:
        DeviceClusterService(session).delete_cluster(cluster_id)
    except Exception as e:
        raise e



@router.get("/uuid/{device_uuid}", status_code=status.HTTP_200_OK, response_model=DeviceResponse, dependencies=[Depends(security)])
def get_device_by_uuid(device_uuid: str, session: Session = Depends(get_db)):
    try:
        return DeviceService(session).get_device_by_uuid(device_uuid)
    except Exception as e:
        raise e


@router.post("/uuid/{device_uuid}/sensor-readings", status_code=status.HTTP_201_CREATED, response_model=SensorDataResponse, dependencies=[Depends(security)])
def ingest_sensor_reading(device_uuid: str, payload: SensorDataPayload, session: Session = Depends(get_db)):
    try:
        return DeviceService(session).ingest_sensor_reading(device_uuid, payload)
    except Exception as e:
        raise e


@router.get("/uuid/{device_uuid}/sensor-readings", status_code=status.HTTP_200_OK, response_model=Page[SensorDataResponse], dependencies=[Depends(security)])
def get_sensor_readings(
    device_uuid: str,
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    try:
        return DeviceService(session).get_sensor_readings(device_uuid, page=page, page_size=page_size)
    except Exception as e:
        raise e


@router.post("/uuid/{device_uuid}/mosquito-events", status_code=status.HTTP_201_CREATED, response_model=MosquitoIndividualResponse, dependencies=[Depends(security)])
def ingest_mosquito_event(device_uuid: str, payload: MosquitoEventPayload, session: Session = Depends(get_db)):
    try:
        return DeviceService(session).ingest_mosquito_event(device_uuid, payload)
    except Exception as e:
        raise e


@router.get("/uuid/{device_uuid}/mosquito-events", status_code=status.HTTP_200_OK, response_model=Page[MosquitoEventResponse], dependencies=[Depends(security)])
def get_mosquito_events(
    device_uuid: str,
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
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
        return DeviceService(session).get_mosquito_events(
            device_uuid,
            page=page,
            page_size=page_size,
            start_date=start_date,
            end_date=end_date,
            search=search,
        )
    except Exception as e:
        raise e


@router.delete("/uuid/{device_uuid}/mosquito-events/{event_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(security)])
def delete_mosquito_event(device_uuid: str, event_id: int, session: Session = Depends(get_db)):
    try:
        DeviceService(session).delete_mosquito_event(device_uuid=device_uuid, event_id=event_id)
    except Exception as e:
        raise e



@router.get("/{device_id}/charts", status_code=status.HTTP_200_OK, response_model=DeviceChartsResponse, dependencies=[Depends(security)])
def get_device_charts(
    device_id: int,
    session: Session = Depends(get_db),
    group_by: str = Query(default="month", pattern="^(hour|day|week|month|year)$"),
):
    try:
        return DeviceChartService(session).get_device_charts(device_id, group_by)
    except Exception as e:
        raise e


@router.get("/{device_id}", status_code=status.HTTP_200_OK, response_model=DeviceResponse, dependencies=[Depends(security)])
def get_device_by_id(device_id: int, session: Session = Depends(get_db)):
    try:
        return DeviceService(session).get_device_by_id(device_id)
    except Exception as e:
        raise e


@router.patch("/{device_id}", status_code=status.HTTP_200_OK, response_model=DeviceResponse, dependencies=[Depends(security)])
def update_device(device_id: int, device_data: DeviceUpdate, session: Session = Depends(get_db)):
    try:
        return DeviceService(session).update_device(device_id, device_data)
    except Exception as e:
        raise e


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(security)])
def delete_device(device_id: int, session: Session = Depends(get_db)):
    try:
        DeviceService(session).delete_device(device_id)
    except Exception as e:
        raise e
