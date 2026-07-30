from fastapi import HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from types import SimpleNamespace
from typing import List, Optional

from app.core.pagination import Page, paginate
from app.device.repository.device_repository import DeviceRepository
from app.notification.events import NotificationEvent, emit
from app.device.schema import (
    DeviceCreate, DeviceResponse, DeviceUpdate,
    SensorDataPayload, SensorDataResponse,
    MosquitoEventPayload, MosquitoIndividualResponse, MosquitoEventResponse,
)


class DeviceService:
    def __init__(self, session: Session):
        self.session = session
        self.device_repository = DeviceRepository(session)

    @staticmethod
    def _assert_scope(device, allowed_cluster_ids: Optional[set]) -> None:
        """404 when a cluster-scoped caller reaches a device outside their scope.

        None allowed set = unrestricted (super admin). We raise 404 rather than
        403 so the existence of another cluster's device is never revealed.
        """
        if allowed_cluster_ids is None:
            return
        if device is None or device.cluster_id not in allowed_cluster_ids:
            raise HTTPException(status_code=404, detail="Device not found")

    def create_device(self, device_data: DeviceCreate) -> DeviceResponse:
        if (
            device_data.device_uuid
            and self.device_repository.device_exists_by_uuid(device_data.device_uuid)
        ):
            raise HTTPException(status_code=400, detail="Device already exists")
        device = self.device_repository.create_device(device_data)
        response = DeviceResponse.model_validate(device)
        if device.cluster is not None:
            emit(self.session, NotificationEvent.CLUSTER_DEVICE_ADDED,
                 cluster=device.cluster, device=device)
        return response

    def update_device(self, device_id: int, device_data: DeviceUpdate) -> DeviceResponse:
        existing = self.device_repository.get_by_id(device_id)
        old_cluster_id = existing.cluster_id if existing else None
        try:
            device = self.device_repository.update_device(device_id, device_data)
        except ValueError:
            raise HTTPException(status_code=404, detail="Device not found")
        response = DeviceResponse.model_validate(device)
        if device.cluster_id != old_cluster_id:
            emit(self.session, NotificationEvent.DEVICE_REASSIGNED,
                 device=device, previous_cluster_id=old_cluster_id)
        return response

    def get_devices(self, page: int = 1, page_size: int = 20,
                    name=None, region=None, search=None, max_mosquito_count=None,
                    min_mosquito_count=None, created_after=None,
                    longitude=None, latitude=None, cluster_id=None, device_uuid=None,
                    trap_status=None, allowed_cluster_ids: Optional[set] = None) -> Page[DeviceResponse]:
        if any(v is not None for v in [name, region, search, device_uuid, max_mosquito_count, min_mosquito_count,
                                        created_after, longitude, latitude, cluster_id, trap_status]):
            devices = self.device_repository.filter_devices(
                name=name, region=region, search=search, max_mosquito_count=max_mosquito_count,
                min_mosquito_count=min_mosquito_count, created_after=created_after,
                longitude=longitude, latitude=latitude, cluster_id=cluster_id, device_uuid=device_uuid,
                trap_status=trap_status,
            )
        else:
            devices = self.device_repository.get_all()
        # Cluster scope: a restricted caller only ever sees devices in their
        # allowed clusters, regardless of the filters they passed.
        if allowed_cluster_ids is not None:
            devices = [d for d in devices if d.cluster_id in allowed_cluster_ids]
        sliced, total, total_pages = paginate(devices, page, page_size)
        return Page[DeviceResponse](
            items=[DeviceResponse.model_validate(d) for d in sliced],
            total=total, page=page, page_size=page_size, total_pages=total_pages,
        )

    def get_device_by_id(self, device_id: int, allowed_cluster_ids: Optional[set] = None) -> DeviceResponse:
        device = self.device_repository.get_by_id(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        self._assert_scope(device, allowed_cluster_ids)
        return DeviceResponse.model_validate(device)

    def delete_device(self, device_id: int) -> None:
        # Snapshot cluster + device identity BEFORE deletion: the ORM objects
        # are unusable after the delete commits. The device snapshot uses
        # id=None because notifications.device_id is a hard FK to devices.id —
        # the deleted row's id would make the notification insert fail.
        device = self.device_repository.get_by_id(device_id)
        cluster_snapshot = device_snapshot = None
        if device is not None and device.cluster is not None:
            cluster_snapshot = SimpleNamespace(id=device.cluster.id, name=device.cluster.name)
            device_snapshot = SimpleNamespace(id=None, name=device.name,
                                              device_uuid=device.device_uuid)
        try:
            self.device_repository.delete_device(device_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Device not found")
        if cluster_snapshot is not None:
            emit(self.session, NotificationEvent.CLUSTER_DEVICE_REMOVED,
                 cluster=cluster_snapshot, device=device_snapshot)

    def get_device_by_uuid(self, device_uuid: str, allowed_cluster_ids: Optional[set] = None) -> DeviceResponse:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        self._assert_scope(device, allowed_cluster_ids)
        return DeviceResponse.model_validate(device)

    def update_total_mosquito_count(self, device_id: int, count: int) -> None:
        try:
            self.device_repository.update_total_mosquito_count(device_id, count)
        except ValueError:
            raise HTTPException(status_code=404, detail="Device not found")

    def refresh_last_activity(self, device_id: int) -> None:
        try:
            self.device_repository.refresh_last_activity(device_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Device not found")


    def ingest_sensor_reading(self, device_uuid: str, payload: SensorDataPayload,
                              allowed_cluster_ids: Optional[set] = None) -> SensorDataResponse:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        self._assert_scope(device, allowed_cluster_ids)
        reading = self.device_repository.create_sensor_reading(device, payload)
        return SensorDataResponse.model_validate(reading)

    def get_sensor_readings(self, device_uuid: str, page: int = 1, page_size: int = 20,
                            allowed_cluster_ids: Optional[set] = None) -> Page[SensorDataResponse]:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        self._assert_scope(device, allowed_cluster_ids)
        readings = self.device_repository.get_sensor_readings(device.id)
        sliced, total, total_pages = paginate(readings, page, page_size)
        return Page[SensorDataResponse](
            items=[SensorDataResponse.model_validate(r) for r in sliced],
            total=total, page=page, page_size=page_size, total_pages=total_pages,
        )


    def ingest_mosquito_event(self, device_uuid: str, payload: MosquitoEventPayload,
                              allowed_cluster_ids: Optional[set] = None) -> MosquitoIndividualResponse:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        self._assert_scope(device, allowed_cluster_ids)
        event = self.device_repository.create_mosquito_event(device, payload)
        if not event.mosquito_reading:
            raise HTTPException(status_code=500, detail="Mosquito reading was not created")
        return MosquitoIndividualResponse.model_validate(event.mosquito_reading)

    def get_mosquito_events(
        self,
        device_uuid: str,
        page: int = 1,
        page_size: int = 20,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        search: str | None = None,
        allowed_cluster_ids: Optional[set] = None,
    ) -> Page[MosquitoEventResponse]:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        self._assert_scope(device, allowed_cluster_ids)
        events = self.device_repository.get_mosquito_events(
            device.id,
            start_date=start_date,
            end_date=end_date,
            search=search,
        )
        sliced, total, total_pages = paginate(events, page, page_size)
        return Page[MosquitoEventResponse](
            items=[MosquitoEventResponse.model_validate(event) for event in sliced],
            total=total, page=page, page_size=page_size, total_pages=total_pages,
        )

    def get_mosquito_filter_options(self, allowed_cluster_ids: Optional[set] = None) -> dict:
        return self.device_repository.get_mosquito_filter_options(allowed_cluster_ids=allowed_cluster_ids)

    def get_all_mosquito_events(
        self,
        page: int = 1,
        page_size: int = 20,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        search: str | None = None,
        region: str | List[str] | None = None,
        device_uuids: List[str] | None = None,
        genus: str | List[str] | None = None,
        species: str | List[str] | None = None,
        allowed_cluster_ids: Optional[set] = None,
    ) -> Page[MosquitoEventResponse]:
        events = self.device_repository.get_all_mosquito_events(
            start_date=start_date,
            end_date=end_date,
            search=search,
            region=region,
            device_uuids=device_uuids,
            genus=genus,
            species=species,
            allowed_cluster_ids=allowed_cluster_ids,
        )
        sliced, total, total_pages = paginate(events, page, page_size)
        return Page[MosquitoEventResponse](
            items=[MosquitoEventResponse.model_validate(event) for event in sliced],
            total=total, page=page, page_size=page_size, total_pages=total_pages,
        )

    def delete_mosquito_event(self, device_uuid: str, event_id: int,
                              allowed_cluster_ids: Optional[set] = None) -> None:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        self._assert_scope(device, allowed_cluster_ids)
        self.device_repository.delete_mosquito_event(device_id=device.id, event_id=event_id)


    def get_all_mosquito_readings(self) -> List[MosquitoIndividualResponse]:
        readings = self.device_repository.get_all_mosquito_readings()
        return [MosquitoIndividualResponse.model_validate(r) for r in readings]
