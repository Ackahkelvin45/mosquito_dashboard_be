from sqlalchemy.orm import Session
from app.device.repository import  repository
from app.device import schema, models

def create_sensor_reading(db: Session, device_id: int, reading: schema.SensorDeviceReadingCreate):
    repo =repository.SensorDeviceReadingRepository(db)
    return repo.create(device_id=device_id, data=reading.dict())

def create_mosquito_event(db: Session, device_id: int, event: schema.MosquitoEventCreate):
    repo = repository.MosquitoEventRepository(db)
    return repo.create(device_id=device_id, event_data=event.dict())