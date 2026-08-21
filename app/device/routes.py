from fastapi import APIRouter, Depends, Query, BackgroundTasks, HTTPException
from fastapi.security import HTTPBearer
from app.device.models import Device, UnregisteredDeviceSighting
from app.device.schema import (
    DeviceCreate, DeviceResponse, DeviceUpdate,
    DeviceClusterCreate, DeviceClusterResponse, DeviceClusterUpdate,
    MosquitoEventPayload, SensorDataPayload, SensorDataResponse,
    SensorReadingWithDeviceResponse, UnregisteredSightingResponse,
    MosquitoIndividualPayload, MosquitoIndividualResponse, MosquitoEventResponse,
)
from app.device.chart_schema import DeviceChartsResponse
from app.audit.models import AuditAction
from app.audit.recorder import audit
from app.core.database import get_db
from app.core.pagination import Page
from sqlalchemy.orm import Session
from utils.protected_route import get_current_user, get_current_user_or_guest
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
        created = DeviceService(session).create_device(device_data)
        audit(session, AuditAction.DEVICE_CREATED, actor_user_id=current_user.id,
              actor_email=current_user.email, target_type="device", target_id=created.id,
              detail={"name": created.name, "device_uuid": created.device_uuid})
        return created
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
    # Read-only listing is open to guests, scoped to public clusters.
    current_user: UserResponse = Depends(get_current_user_or_guest),
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
        updated = DeviceService(session).update_device(device_id, device_data)
        audit(session, AuditAction.DEVICE_UPDATED, actor_user_id=current_user.id,
              actor_email=current_user.email, target_type="device", target_id=device_id,
              detail={"fields": sorted(device_data.model_fields_set)})
        return updated
    except Exception as e:
        raise e


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(device_id: int, session: Session = Depends(get_db),
                  current_user: UserResponse = Depends(require_super_admin)):
    try:
        DeviceService(session).delete_device(device_id)
        audit(session, AuditAction.DEVICE_DELETED, actor_user_id=current_user.id,
              actor_email=current_user.email, target_type="device", target_id=device_id)
    except Exception as e:
        raise e


# ── Clusters ─────────────────────────────────────────────────────────────────

@router.get("/clusters", status_code=status.HTTP_200_OK, response_model=Page[DeviceClusterResponse])
def get_clusters(
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: UserResponse = Depends(get_current_user_or_guest),
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
        created = DeviceClusterService(session).create_cluster(cluster_data)
        audit(session, AuditAction.CLUSTER_CREATED, actor_user_id=current_user.id,
              actor_email=current_user.email, target_type="cluster", target_id=created.id,
              detail={"name": created.name, "public": created.public})
        return created
    except Exception as e:
        raise e


@router.get("/clusters/{cluster_id}", status_code=status.HTTP_200_OK, response_model=DeviceClusterResponse)
def get_cluster_by_id(cluster_id: int, session: Session = Depends(get_db),
                      current_user: UserResponse = Depends(get_current_user_or_guest)):
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
        updated = DeviceClusterService(session).update_cluster(cluster_id, cluster_data)
        audit(session, AuditAction.CLUSTER_UPDATED, actor_user_id=current_user.id,
              actor_email=current_user.email, target_type="cluster", target_id=cluster_id,
              detail={"fields": sorted(cluster_data.model_fields_set)})
        return updated
    except Exception as e:
        raise e


@router.delete("/clusters/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cluster(cluster_id: int, session: Session = Depends(get_db),
                   current_user: UserResponse = Depends(require_super_admin)):
    try:
        DeviceClusterService(session).delete_cluster(cluster_id)
        audit(session, AuditAction.CLUSTER_DELETED, actor_user_id=current_user.id,
              actor_email=current_user.email, target_type="cluster", target_id=cluster_id)
    except Exception as e:
        raise e


# ── Device data by UUID ──────────────────────────────────────────────────────

@router.get("/uuid/{device_uuid}", status_code=status.HTTP_200_OK, response_model=DeviceResponse)
def get_device_by_uuid(device_uuid: str, session: Session = Depends(get_db),
                       current_user: UserResponse = Depends(get_current_user_or_guest)):
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


@router.get("/sensor-readings", status_code=status.HTTP_200_OK, response_model=Page[SensorReadingWithDeviceResponse])
def get_all_sensor_readings(
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    start_date: Optional[datetime] = Query(default=None),
    end_date: Optional[datetime] = Query(default=None),
    search: Optional[str] = Query(default=None, description="Matches device name, UUID or region"),
    region: Optional[List[str]] = Query(default=None, description="Repeatable: matches any of the given regions"),
    device_uuid: Optional[List[str]] = Query(default=None),
    cluster_id: Optional[List[int]] = Query(default=None, description="Repeatable: matches any of the given clusters (super admin only — intersected with your own scope otherwise)"),
    current_user: UserResponse = Depends(get_current_user_or_guest),
):
    """Fleet-wide sensor readings for the Sensor Data page, newest first."""
    try:
        allowed = visible_cluster_ids(session, current_user)
        return DeviceService(session).get_all_sensor_readings(
            page=page, page_size=page_size,
            start_date=start_date, end_date=end_date, search=search,
            region=region, device_uuids=device_uuid, cluster_id=cluster_id,
            allowed_cluster_ids=allowed,
        )
    except Exception as e:
        raise e


@router.get("/uuid/{device_uuid}/sensor-readings", status_code=status.HTTP_200_OK, response_model=Page[SensorDataResponse])
def get_sensor_readings(
    device_uuid: str,
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: UserResponse = Depends(get_current_user_or_guest),
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
    current_user: UserResponse = Depends(get_current_user_or_guest),
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
        audit(session, AuditAction.MOSQUITO_EVENT_DELETED, actor_user_id=current_user.id,
              actor_email=current_user.email, target_type="mosquito_event", target_id=event_id,
              detail={"device_uuid": device_uuid})
    except Exception as e:
        raise e


# ── Unregistered sightings (TODO.md §1) ──────────────────────────────────────
# Registered BEFORE the /{device_id} routes: "unregistered" must never be
# swallowed by the int path parameter (it would 422, not fall through).

@router.get("/unregistered", status_code=status.HTTP_200_OK,
            response_model=Page[UnregisteredSightingResponse])
def list_unregistered_sightings(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(require_super_admin),
):
    """UUIDs currently publishing MQTT data with no registered device —
    only super admins can act on them (registration is super-admin-only)."""
    q = (session.query(UnregisteredDeviceSighting)
         .order_by(UnregisteredDeviceSighting.last_seen.desc()))
    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return Page(
        items=[UnregisteredSightingResponse.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size, total_pages=total_pages,
    )


@router.delete("/unregistered/{device_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_unregistered_sighting(
    device_uuid: str,
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(require_super_admin),
):
    """Dismiss a stray UUID (e.g. a neighbour's test device). It will
    reappear if it keeps publishing — deliberate: an unhandled publisher
    should keep nagging, not vanish forever."""
    deleted = (
        session.query(UnregisteredDeviceSighting)
        .filter(UnregisteredDeviceSighting.device_uuid == device_uuid)
        .delete(synchronize_session=False)
    )
    session.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="Sighting not found")
    audit(session, AuditAction.SIGHTING_DISMISSED, actor_user_id=current_user.id,
          actor_email=current_user.email, target_type="sighting", target_id=device_uuid)


# ── Per-device charts ────────────────────────────────────────────────────────

@router.get("/{device_id}/charts", status_code=status.HTTP_200_OK, response_model=DeviceChartsResponse)
def get_device_charts(
    device_id: int,
    session: Session = Depends(get_db),
    group_by: str = Query(default="month", pattern="^(hour|day|week|month|year)$"),
    current_user: UserResponse = Depends(get_current_user_or_guest),
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
                     current_user: UserResponse = Depends(get_current_user_or_guest)):
    try:
        allowed = visible_cluster_ids(session, current_user)
        return DeviceService(session).get_device_by_id(device_id, allowed_cluster_ids=allowed)
    except Exception as e:
        raise e

# PATCH/DELETE "/{device_id}" are registered near the top of this module.
