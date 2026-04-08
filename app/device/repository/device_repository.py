from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, func
from typing import Optional, List
from app.crud.base import BaseRepository
from app.device.models import Device,DeviceCluster,SensorDeviceReading,MosquitoEvent,MosquitoIndividualReading
from app.device.schema import DeviceCreate, DeviceUpdate,SensorDataPayload,MosquitoEventPayload
from datetime import datetime, timezone
from fastapi import HTTPException
from utils.time_range import to_utc_naive


class DeviceRepository(BaseRepository[Device]):
    model = Device

    def create_device(self, device_data: DeviceCreate) -> Device:
        data = device_data.model_dump(exclude_none=True)

        cluster = (
            self.session.query(DeviceCluster)
            .filter(DeviceCluster.id == device_data.cluster_id)
            .first()
        )
        if not cluster:
            raise HTTPException(status_code=404, detail=f"Cluster with id {device_data.cluster_id} not found")
        new_device = Device(**data)
        self.session.add(new_device)
        self.session.commit()
        self.session.refresh(new_device)

        return new_device

    def update_device(self, device_id: int, device_data: DeviceUpdate) -> Device:
        device = self.get_by_id(device_id)

        if not device:
            raise ValueError("Device not found")

        update_data = device_data.model_dump(exclude_none=True)
        if "cluster_id" in update_data:
            cluster = (
                self.session.query(DeviceCluster)
                .filter(DeviceCluster.id == update_data["cluster_id"])
                .first()
            )
            if not cluster:
                raise HTTPException(status_code=404, detail=f"Cluster with id {update_data['cluster_id']} not found")
 

        for key, value in update_data.items():
            setattr(device, key, value)

        self.session.commit()
        self.session.refresh(device)

        return device

    def get_by_uuid(self, device_uuid: str) -> Optional[Device]:
        return (
            self.session.query(Device)
            .filter(Device.device_uuid == device_uuid)
            .first()
        )

    def get_by_id(self, device_id: int) -> Optional[Device]:
        return (
            self.session.query(Device)
            .filter(Device.id == device_id)
            .first()
        )

    def get_all(self) -> List[Device]:
        return self.session.query(Device).all()

    def device_exists_by_uuid(self, device_uuid: str) -> bool:
        return (
            self.session.query(Device)
            .filter(Device.device_uuid == device_uuid)
            .first()
            is not None
        )

    def delete_device(self, device_id: int) -> None:
        print(f"Attempting to delete device with id: {device_id}")
        device = self.get_by_id(device_id)
        

        if not device:
            raise ValueError("Device not found")

        self.session.delete(device)
        self.session.commit()

    def update_total_mosquito_count(self, device_id: int, count: int) -> None:
        device = self.get_by_id(device_id)

        if not device:
            raise ValueError("Device not found")

        device.total_mosquito_count = (device.total_mosquito_count or 0) + count
        self.session.commit()

    
    def filter_devices(
            self,
            region: Optional[str] = None,
            name: Optional[str] = None,
            device_uuid: Optional[str] = None,
            min_mosquito_count: Optional[int] = None,
            max_mosquito_count: Optional[int] = None,
            latitude: Optional[float] = None,
            longitude: Optional[float] = None,
            cluster_id: Optional[int] = None,
            created_after: Optional[datetime] = None,
    ) -> List[Device]:
        query = self.session.query(Device)

        if region:
            query = query.filter(Device.region.ilike(f"%{region}%"))
        if name:
            query = query.filter(Device.name.ilike(f"%{name}%"))
        if device_uuid:
            query = query.filter(Device.device_uuid == device_uuid)
        if min_mosquito_count is not None:
            query = query.filter(Device.total_mosquito_count >= min_mosquito_count)
        if max_mosquito_count is not None:
            query = query.filter(Device.total_mosquito_count <= max_mosquito_count)
        if latitude is not None:
            query = query.filter(Device.latitude == latitude)
        if longitude is not None:
            query = query.filter(Device.longitude == longitude)
        if created_after is not None:
            query = query.filter(Device.created_at >= created_after)
        if cluster_id is not None:
            query = query.filter(Device.cluster_id == cluster_id)

        return query.all()
    
    def refresh_last_activity(self, device_id: int) -> None:
        device = self.get_by_id(device_id)
        if not device:
            raise ValueError("Device not found")
        device.last_activity = datetime.now(timezone.utc)
        self.session.commit()



    def create_sensor_reading(self, device: Device, payload: SensorDataPayload) -> SensorDeviceReading:
            reading = SensorDeviceReading(
                device_id=device.id,
                timestamp=payload.timestamp,
                external_temperature=payload.temp_external,
                internal_temperature=payload.temp_internal,
                external_humidity=payload.humidity_external,
                internal_humidity=payload.humidity_internal,
                internal_pressure=payload.pressure_internal,
                external_pressure=payload.pressure_external,
                external_light=payload.external_light,
                battery_voltage=payload.battery,
                trap_status=payload.trap_status,
            )
            device.last_activity = payload.timestamp
            self.session.add(reading)
            self.session.commit()
            self.session.refresh(reading)
            return reading

    

    def get_sensor_readings(self, device_id: int) -> List[SensorDeviceReading]:
        return (
            self.session.query(SensorDeviceReading)
            .filter(SensorDeviceReading.device_id == device_id)
            .order_by(SensorDeviceReading.timestamp.desc())
            .all()
        )

    def create_mosquito_event(self, device: Device, payload: MosquitoEventPayload) -> MosquitoEvent:
        reading_payload = payload.mosquito_reading
        mosquito_reading = MosquitoIndividualReading(
            detection_timestamp=reading_payload.detection_timestamp,
            species=reading_payload.species,
            genus=reading_payload.genus,
            age_group=reading_payload.age_group,
            sex=reading_payload.sex,
        )
        event = MosquitoEvent(
            device_id=device.id,
            timestamp=payload.timestamp,
            count=1,
            mosquito_reading=mosquito_reading,
        )
        device.total_mosquito_count = (device.total_mosquito_count or 0) + 1
        device.last_activity = payload.timestamp
        self.session.add(event)
        self.session.commit()
        self.session.refresh(event)
        return event
    


    def _apply_mosquito_event_filters(
        self,
        query,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        search: str | None = None,
    ):
        if start_date is not None:
            start_date = to_utc_naive(start_date)
        if end_date is not None:
            end_date = to_utc_naive(end_date)
        if start_date is not None:
            query = query.filter(MosquitoEvent.timestamp >= start_date)
        if end_date is not None:
            query = query.filter(MosquitoEvent.timestamp <= end_date)

        if search:
            tokens = [t for t in search.strip().split() if t]
            for token in tokens:
                like = f"%{token}%"
                query = query.filter(
                    or_(
                        func.coalesce(Device.device_uuid, "").ilike(like),
                        func.coalesce(MosquitoIndividualReading.species, "").ilike(like),
                        func.coalesce(MosquitoIndividualReading.genus, "").ilike(like),
                        func.coalesce(MosquitoIndividualReading.age_group, "").ilike(like),
                        func.coalesce(MosquitoIndividualReading.sex, "").ilike(like),
                    )
                )

        return query

    def get_mosquito_events(
        self,
        device_id: int,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        search: str | None = None,
    ) -> List[MosquitoEvent]:
        base_query = (
            self.session.query(MosquitoEvent)
            .filter(MosquitoEvent.device_id == device_id)
        )
        base_query = self._apply_mosquito_event_filters(
            base_query,
            start_date=start_date,
            end_date=end_date,
            search=None,
        )

        if not search:
            return (
                base_query
                .options(joinedload(MosquitoEvent.mosquito_reading))
                .order_by(MosquitoEvent.timestamp.desc())
                .all()
            )

        # When searching, join related tables for filtering and dedupe via a DISTINCT ON subquery
        # (required for Postgres correctness).
        search_query = (
            self.session.query(MosquitoEvent.id)
            .select_from(MosquitoEvent)
            .join(Device, MosquitoEvent.device_id == Device.id)
            .outerjoin(MosquitoIndividualReading, MosquitoIndividualReading.batch_id == MosquitoEvent.id)
            .filter(MosquitoEvent.device_id == device_id)
        )
        search_query = self._apply_mosquito_event_filters(
            search_query,
            start_date=start_date,
            end_date=end_date,
            search=search,
        )
        ids_subq = (
            search_query
            .distinct(MosquitoEvent.id)
            .order_by(MosquitoEvent.id, MosquitoEvent.timestamp.desc())
            .subquery()
        )

        return (
            self.session.query(MosquitoEvent)
            .join(ids_subq, MosquitoEvent.id == ids_subq.c.id)
            .options(joinedload(MosquitoEvent.mosquito_reading))
            .order_by(MosquitoEvent.timestamp.desc())
            .all()
        )

    def get_all_mosquito_events(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        search: str | None = None,
    ) -> List[MosquitoEvent]:
        base_query = self.session.query(MosquitoEvent)
        base_query = self._apply_mosquito_event_filters(
            base_query,
            start_date=start_date,
            end_date=end_date,
            search=None,
        )

        if not search:
            return (
                base_query
                .options(
                    joinedload(MosquitoEvent.device),
                    joinedload(MosquitoEvent.mosquito_reading),
                )
                .order_by(MosquitoEvent.timestamp.desc())
                .all()
            )

        search_query = (
            self.session.query(MosquitoEvent.id)
            .select_from(MosquitoEvent)
            .join(Device, MosquitoEvent.device_id == Device.id)
            .outerjoin(MosquitoIndividualReading, MosquitoIndividualReading.batch_id == MosquitoEvent.id)
        )
        search_query = self._apply_mosquito_event_filters(
            search_query,
            start_date=start_date,
            end_date=end_date,
            search=search,
        )
        ids_subq = (
            search_query
            .distinct(MosquitoEvent.id)
            .order_by(MosquitoEvent.id, MosquitoEvent.timestamp.desc())
            .subquery()
        )

        return (
            self.session.query(MosquitoEvent)
            .join(ids_subq, MosquitoEvent.id == ids_subq.c.id)
            .options(
                joinedload(MosquitoEvent.device),
                joinedload(MosquitoEvent.mosquito_reading),
            )
            .order_by(MosquitoEvent.timestamp.desc())
            .all()
        )

    def get_mosquito_event_by_id(self, device_id: int, event_id: int) -> MosquitoEvent | None:
        return (
            self.session.query(MosquitoEvent)
            .filter(MosquitoEvent.id == event_id, MosquitoEvent.device_id == device_id)
            .first()
        )

    def delete_mosquito_event(self, device_id: int, event_id: int) -> None:
        event = self.get_mosquito_event_by_id(device_id=device_id, event_id=event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Mosquito event not found")

        # Delete ALL readings tied to this event first (older data may have >1 row per event).
        readings_deleted = (
            self.session.query(MosquitoIndividualReading)
            .filter(MosquitoIndividualReading.batch_id == event.id)
            .delete(synchronize_session=False)
        )

        device = self.session.query(Device).filter(Device.id == device_id).first()
        if device and readings_deleted:
            device.total_mosquito_count = max(0, (device.total_mosquito_count or 0) - readings_deleted)

        self.session.delete(event)
        self.session.commit()



    def get_all_mosquito_readings(self)->List[MosquitoIndividualReading]:
        return self.session.query(MosquitoIndividualReading).order_by(MosquitoIndividualReading.detection_timestamp.desc()).all()
