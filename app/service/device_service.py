from fastapi import HTTPException
from sqlalchemy.orm import Session
from app.device.repository.device_repository import DeviceRepository
from app.device.schema import DeviceCreate, DeviceResponse,DeviceUpdate
import datetime

class DeviceService:
    def __init__(self, session: Session):
        self.device_repository = DeviceRepository(session)

    def create_device(self, device_data: DeviceCreate) -> DeviceResponse:
        if device_data.device_uuid and self.device_repository.device_exists_by_uuid(device_data.device_uuid):
            raise HTTPException(status_code=400, detail="Device already exists")
        device = self.device_repository.create_device(device_data)
        return DeviceResponse.model_validate(device)
    

    def update_device(self, device_id: int, device_data: DeviceUpdate) -> DeviceResponse:
        device = self.device_repository.update_device(device_id, device_data)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        return DeviceResponse.model_validate(device)

    def get_devices(self) -> list[DeviceResponse]:
        devices = self.device_repository.get_all()
        return [DeviceResponse.model_validate(device) for device in devices]

    def get_device_by_id(self, device_id: int) -> DeviceResponse:
        device = self.device_repository.get_by_id(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        return DeviceResponse.model_validate(device)

    def get_device_by_uuid(self, device_uuid: str) -> DeviceResponse:
        device = self.device_repository.get_by_uuid(device_uuid)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        return DeviceResponse.model_validate(device)

    def update_total_mosquito_count(self, device_id: int, count: int) -> None:
        device = self.device_repository.get_by_id(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        self.device_repository.update_total_mosquito_count(device_id, count)

    def refresh_last_activity(self, device_id: int) -> None:
        device = self.device_repository.get_by_id(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        device.last_activity = datetime.utcnow()
        self.device_repository.session.commit()