from fastapi import HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from typing import List

from app.device.repository.device_repository import DeviceRepository
from app.device.schema import (
    DeviceCreate, DeviceResponse, DeviceUpdate,
    SensorDataPayload, SensorDataResponse,
    MosquitoEventPayload, MosquitoIndividualResponse,
)


class DeviceService:
    def __init__(self, session: Session):
        self.device_repository = DeviceRepository(session)


    def create_device(self, device_data: DeviceCreate) -> DeviceResponse:
        if (
            device_data.device_uuid
            and self.device_repository.device_exists_by_uuid(device_data.device_uuid)
        ):
            raise HTTPException(status_code=400, detail="Device already exists")
        device = self.device_repository.create_device(device_data)
        return DeviceResponse.model_validate(device)

    def update_device(self, device_id: int, device_data: DeviceUpdate) -> DeviceResponse:
        try:
            device = self.device_repository.update_device(device_id, device_data)
        except ValueError:
            raise HTTPException(status_code=404, detail="Device not found")
        return DeviceResponse.model_validate(device)

    def get_devices(self, name=None, region=None, max_mosquito_count=None,
                    min_mosquito_count=None, created_after=None,
                    longitude=None, latitude=None, cluster_id=None) -> List[DeviceResponse]:
        if any(v is not None for v in [name, region, max_mosquito_count, min_mosquito_count,
                                        created_after, longitude, latitude, cluster_id]):
            devices = self.device_repository.filter_devices(
                name=name, region=region, max_mosquito_count=max_mosquito_count,
                min_mosquito_count=min_mosquito_count, created_after=created_after,
                longitude=longitude, latitude=latitude, cluster_id=cluster_id,
            )
        else:
            devices = self.device_repository.get_all()
        return [DeviceResponse.model_validate(d) for d in devices]

    def get_device_by_id(self, device_id: int) -> DeviceResponse:
        device = self.device_repository.get_by_id(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        return DeviceResponse.model_validate(device)

    def delete_device(self, device_id: int) -> None:
        try:
            self.device_repository.delete_device(device_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Device not found")

    def get_device_by_uuid(self, device_uuid: str) -> DeviceResponse:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
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


    def ingest_sensor_reading(self, device_uuid: str, payload: SensorDataPayload) -> SensorDataResponse:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        reading = self.device_repository.create_sensor_reading(device, payload)
        return SensorDataResponse.model_validate(reading)

    def get_sensor_readings(self, device_uuid: str) -> List[SensorDataResponse]:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        readings = self.device_repository.get_sensor_readings(device.id)
        return [SensorDataResponse.model_validate(r) for r in readings]


    def ingest_mosquito_event(self, device_uuid: str, payload: MosquitoEventPayload) -> List[MosquitoIndividualResponse]:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        event = self.device_repository.create_mosquito_event(device, payload)
        return [MosquitoIndividualResponse.model_validate(r) for r in event.individual_readings]

    def get_mosquito_events(self, device_uuid: str) -> List[MosquitoIndividualResponse]:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        events = self.device_repository.get_mosquito_events(device.id)
        return [
            MosquitoIndividualResponse.model_validate(r)
            for event in events
            for r in event.individual_readings
        ]