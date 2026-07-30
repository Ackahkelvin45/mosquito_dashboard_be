from fastapi import APIRouter, Depends, Query, BackgroundTasks, HTTPException
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
from app.authentication.schema import UserResponse
from app.core.security.permissions import (
    require_super_admin, require_admin, visible_cluster_ids, is_super_admin,
)
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


# ── Devices ──────────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED, response_model=DeviceResponse)
def create_device(device_data: DeviceCreate, session: Session = Depends(get_db),
                  current_user: UserResponse = Depends(require_super_admin)):
    # Only a super admin registers devices.
    try:
        return DeviceService(session).create_device(device_data)
    except Exception as e:
        raise e


@router.get("", status_code=status.HTTP_200_OK, response_model=Page[DeviceResponse])
def get_devices(
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    name: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
    device_uuid: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None, description="General search across name, region, community and UUID"),
    max_mosquito_count: Optional[int] = Query(default=None),
    min_mosquito_count: Optional[int] = Query(default=None),
    created_after: Optional[datetime] = Query(default=None),
    longitude: Optional[float] = Query(default=None),
    latitude: Optional[float] = Query(default=None),
    cluster_id: Optional[List[int]] = Query(default=None, description="Repeatable: matches devices in any of the given clusters"),
    trap_status: Optional[bool] = Query(default=None, description="Filter by device on/off status (latest reading's trap_status)"),
    current_user: UserResponse = Depends(get_current_user),
):
    try:
        allowed = visible_cluster_ids(session, current_user)
        return DeviceService(session).get_devices(
            page=page, page_size=page_size,
            name=name, region=region,
            device_uuid=device_uuid, search=search,
            max_mosquito_count=max_mosquito_count, min_mosquito_count=min_mosquito_count,
            created_after=created_after, longitude=longitude,
            latitude=latitude, cluster_id=cluster_id,
            trap_status=trap_status,
            allowed_cluster_ids=allowed,
        )
    except Exception as e:
        raise e


@router.patch("/{device_id}", status_code=status.HTTP_200_OK, response_model=DeviceResponse)
def update_device(device_id: int, device_data: DeviceUpdate, session: Session = Depends(get_db),
                  current_user: UserResponse = Depends(require_admin)):
    # Super admin edits any device; a cluster admin only devices in their own
    # cluster (public clusters are visible but not "theirs" to edit).
    try:
        allowed = visible_cluster_ids(session, current_user)
        existing = DeviceService(session).get_device_by_id(device_id, allowed_cluster_ids=allowed)
        if not is_super_admin(current_user) and existing.cluster_id != current_user.cluster_id:
            raise HTTPException(status_code=403, detail="You can only edit devices in your own cluster")
        return DeviceService(session).update_device(device_id, device_data)
    except Exception as e:
        raise e


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(device_id: int, session: Session = Depends(get_db),
                  current_user: UserResponse = Depends(require_super_admin)):
    try:
        DeviceService(session).delete_device(device_id)
    except Exception as e:
        raise e


# ── Clusters ─────────────────────────────────────────────────────────────────

@router.get("/clusters", status_code=status.HTTP_200_OK, response_model=Page[DeviceClusterResponse])
def get_clusters(
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: UserResponse = Depends(get_current_user),
):
    try:
        allowed = visible_cluster_ids(session, current_user)
        return DeviceClusterService(session).get_clusters(page=page, page_size=page_size, allowed_cluster_ids=allowed)
    except Exception as e:
        raise e


@router.post("/clusters", status_code=status.HTTP_201_CREATED, response_model=DeviceClusterResponse)
def create_cluster(cluster_data: DeviceClusterCreate, session: Session = Depends(get_db),
                   current_user: UserResponse = Depends(require_super_admin)):
    try:
        return DeviceClusterService(session).create_cluster(cluster_data)
    except Exception as e:
        raise e


@router.get("/clusters/{cluster_id}", status_code=status.HTTP_200_OK, response_model=DeviceClusterResponse)
def get_cluster_by_id(cluster_id: int, session: Session = Depends(get_db),
                      current_user: UserResponse = Depends(get_current_user)):
    try:
        allowed = visible_cluster_ids(session, current_user)
        if allowed is not None and cluster_id not in allowed:
            raise HTTPException(status_code=404, detail="Device cluster not found")
        return DeviceClusterService(session).get_cluster_by_id(cluster_id)
    except Exception as e:
        raise e


@router.patch("/clusters/{cluster_id}", status_code=status.HTTP_200_OK, response_model=DeviceClusterResponse)
def update_cluster(cluster_id: int, cluster_data: DeviceClusterUpdate, session: Session = Depends(get_db),
                   current_user: UserResponse = Depends(require_super_admin)):
    try:
        return DeviceClusterService(session).update_cluster(cluster_id, cluster_data)
    except Exception as e:
        raise e


@router.delete("/clusters/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cluster(cluster_id: int, session: Session = Depends(get_db),
                   current_user: UserResponse = Depends(require_super_admin)):
    try:
        DeviceClusterService(session).delete_cluster(cluster_id)
    except Exception as e:
        raise e


# ── Device data by UUID ──────────────────────────────────────────────────────

@router.get("/uuid/{device_uuid}", status_code=status.HTTP_200_OK, response_model=DeviceResponse)
def get_device_by_uuid(device_uuid: str, session: Session = Depends(get_db),
                       current_user: UserResponse = Depends(get_current_user)):
    try:
        allowed = visible_cluster_ids(session, current_user)
        return DeviceService(session).get_device_by_uuid(device_uuid, allowed_cluster_ids=allowed)
    except Exception as e:
        raise e


@router.post("/uuid/{device_uuid}/sensor-readings", status_code=status.HTTP_201_CREATED, response_model=SensorDataResponse)
def ingest_sensor_reading(device_uuid: str, payload: SensorDataPayload, session: Session = Depends(get_db),
                          current_user: UserResponse = Depends(get_current_user)):
    try:
        allowed = visible_cluster_ids(session, current_user)
        return DeviceService(session).ingest_sensor_reading(device_uuid, payload, allowed_cluster_ids=allowed)
    except Exception as e:
        raise e


@router.get("/uuid/{device_uuid}/sensor-readings", status_code=status.HTTP_200_OK, response_model=Page[SensorDataResponse])
def get_sensor_readings(
    device_uuid: str,
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: UserResponse = Depends(get_current_user),
):
    try:
        allowed = visible_cluster_ids(session, current_user)
        return DeviceService(session).get_sensor_readings(device_uuid, page=page, page_size=page_size, allowed_cluster_ids=allowed)
    except Exception as e:
        raise e


@router.post("/uuid/{device_uuid}/mosquito-events", status_code=status.HTTP_201_CREATED, response_model=MosquitoIndividualResponse)
def ingest_mosquito_event(device_uuid: str, payload: MosquitoEventPayload, session: Session = Depends(get_db),
                          current_user: UserResponse = Depends(get_current_user)):
    try:
        allowed = visible_cluster_ids(session, current_user)
        return DeviceService(session).ingest_mosquito_event(device_uuid, payload, allowed_cluster_ids=allowed)
    except Exception as e:
        raise e


@router.get("/uuid/{device_uuid}/mosquito-events", status_code=status.HTTP_200_OK, response_model=Page[MosquitoEventResponse])
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
    current_user: UserResponse = Depends(get_current_user),
):
    try:
        if start_date is None and end_date is None and range_:
            window_at = at or datetime.now(timezone.utc)
            start_date, end_date = compute_datetime_range(range_, window_at)
        allowed = visible_cluster_ids(session, current_user)
        return DeviceService(session).get_mosquito_events(
            device_uuid,
            page=page,
            page_size=page_size,
            start_date=start_date,
            end_date=end_date,
            search=search,
            allowed_cluster_ids=allowed,
        )
    except Exception as e:
        raise e


@router.delete("/uuid/{device_uuid}/mosquito-events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mosquito_event(device_uuid: str, event_id: int, session: Session = Depends(get_db),
                          current_user: UserResponse = Depends(require_admin)):
    # Deleting data is an admin/super-admin action, scoped to a visible device.
    try:
        allowed = visible_cluster_ids(session, current_user)
        DeviceService(session).delete_mosquito_event(device_uuid=device_uuid, event_id=event_id, allowed_cluster_ids=allowed)
    except Exception as e:
        raise e


# ── Per-device charts ────────────────────────────────────────────────────────

@router.get("/{device_id}/charts", status_code=status.HTTP_200_OK, response_model=DeviceChartsResponse)
def get_device_charts(
    device_id: int,
    session: Session = Depends(get_db),
    group_by: str = Query(default="month", pattern="^(hour|day|week|month|year)$"),
    current_user: UserResponse = Depends(get_current_user),
):
    try:
        # 404 if the device is outside the caller's scope before charting it.
        allowed = visible_cluster_ids(session, current_user)
        DeviceService(session).get_device_by_id(device_id, allowed_cluster_ids=allowed)
        return DeviceChartService(session).get_device_charts(device_id, group_by)
    except Exception as e:
        raise e


@router.get("/{device_id}", status_code=status.HTTP_200_OK, response_model=DeviceResponse)
def get_device_by_id(device_id: int, session: Session = Depends(get_db),
                     current_user: UserResponse = Depends(get_current_user)):
    try:
        allowed = visible_cluster_ids(session, current_user)
        return DeviceService(session).get_device_by_id(device_id, allowed_cluster_ids=allowed)
    except Exception as e:
        raise e

# PATCH/DELETE "/{device_id}" are registered near the top of this module.
