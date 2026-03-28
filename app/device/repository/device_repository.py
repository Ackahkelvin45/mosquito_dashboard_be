from app.crud.base import BaseRepository
from app.device.models import Device
from app.device.schema import DeviceCreate,DeviceUpdate


class DeviceRepository(BaseRepository[Device]):
    model = Device

    def create_device(self, device_data: DeviceCreate) -> Device:
        data = device_data.model_dump(exclude_none=True)
        # Create the device instance
        new_device = Device(**data)
        self.session.add(new_device)
        self.session.commit()
        self.session.refresh(new_device)
        return new_device
    
    def update_device(self, device_id: int, device_data: DeviceUpdate) -> Device:
        device = self.get_by_id(device_id)
        if not device:
            return None
        for key, value in device_data.model_dump(exclude_none=True).items():
            setattr(device, key, value)
        self.session.commit()
        self.session.refresh(device)
        return device

    def get_by_uuid(self, device_uuid: str) -> Device | None:
        return self.session.query(Device).filter(Device.device_uuid == device_uuid).first()

    def get_by_id(self, device_id: int) -> Device | None:
        return self.session.query(Device).filter(Device.id == device_id).first()
    
    def get_all(self) -> list[Device]:
        return self.session.query(Device).all()

    def device_exists_by_uuid(self, device_uuid: str) -> bool:
        return self.session.query(Device).filter(Device.device_uuid == device_uuid).first() is not None
    


    def update_total_mosquito_count(self, device_id: int, count: int) -> None:
        device = self.get_by_id(device_id)
        if device:
            device.total_mosquito_count += count
            self.session.commit()