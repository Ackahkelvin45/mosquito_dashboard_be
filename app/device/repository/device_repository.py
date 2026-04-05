from sqlalchemy.orm import Session
from typing import Optional, List
from app.crud.base import BaseRepository
from app.device.models import Device,DeviceCluster,SensorDeviceReading,MosquitoEvent,MosquitoIndividualReading
from app.device.schema import DeviceCreate, DeviceUpdate,SensorDataPayload,MosquitoEventPayload
from datetime import datetime, timezone
from fastapi import HTTPException


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
        individual_readings = [
            MosquitoIndividualReading(
                detection_timestamp=r.detection_timestamp,
                species=r.species,
                genus=r.genus,
                age_group=r.age_group,
                sex=r.sex,
            )
            for r in payload.mosquito_data
        ]
        event = MosquitoEvent(
            device_id=device.id,
            timestamp=payload.timestamp,
            count=len(individual_readings),
            individual_readings=individual_readings,
        )
        device.total_mosquito_count = (device.total_mosquito_count or 0) + len(individual_readings)
        device.last_activity = payload.timestamp
        self.session.add(event)
        self.session.commit()
        self.session.refresh(event)
        return event
    


    def get_mosquito_events(self, device_id: int) -> List[MosquitoEvent]:
        return (
            self.session.query(MosquitoEvent)
            .filter(MosquitoEvent.device_id == device_id)
            .order_by(MosquitoEvent.timestamp.desc())
            .all()
        )
