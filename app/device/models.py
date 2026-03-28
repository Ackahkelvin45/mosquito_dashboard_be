from app.core.database import Base
from sqlalchemy import Enum, Integer, String, DateTime, Boolean, Float,ForeignKey
from sqlalchemy.orm import relationship, mapped_column, Mapped
from datetime import datetime
from app.authentication.enums import DeviceStatus
import uuid


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    device_uuid=mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(255))
    longitude: Mapped[float] = mapped_column(Float)
    latitude: Mapped[float] = mapped_column(Float)
    region: Mapped[str] = mapped_column(String(100))
    gmap_link: Mapped[str] = mapped_column(String(255))

    last_activity: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    total_mosquito_count: Mapped[int] = mapped_column(Integer, default=0)

    device_reading:Mapped[list["SensorDeviceReading"]]=relationship("SensorDeviceReading", back_populates="device", cascade="all, delete-orphan")
    mosquito_readings:Mapped[list["MosquitoEvent"]]=relationship("MosquitoEvent", back_populates="device", cascade="all, delete-orphan")
 
    def __repr__(self):
        return (
            f"Device(id={self.id}, name={self.name}, longitude={self.longitude}, "
            f"latitude={self.latitude}, last_activity={self.last_activity}, "
            f"created_at={self.created_at}, updated_at={self.updated_at})"
        )

  
    


    


class SensorDeviceReading(Base):
    __tablename__ = "sensor_device_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    device_id: Mapped[int] = mapped_column(Integer, ForeignKey("devices.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    external_temperature: Mapped[float] = mapped_column(Float)
    internal_temperature: Mapped[float] = mapped_column(Float)
    external_humidity: Mapped[float] = mapped_column(Float)
    internal_humidity: Mapped[float] = mapped_column(Float)
    internal_pressure:Mapped[float]=mapped_column(Float)
    external_light:Mapped[float]=mapped_column(Float)
    battery_voltage:Mapped[float]=mapped_column(Float)
    trap_status:Mapped[bool]=mapped_column(Boolean, default=False)
    device: Mapped["Device"] = relationship("Device", back_populates="device_reading")

    
    def __repr__(self):
        return f"SensorDeviceReading(id={self.id}, device_id={self.device_id}, timestamp={self.timestamp}, external_temperature={self.external_temperature}, internal_temperature={self.internal_temperature}, external_humidity={self.external_humidity}, internal_humidity={self.internal_humidity}, internal_pressure={self.internal_pressure}, external_light={self.external_light}, battery_voltage={self.battery_voltage}, trap_status={self.trap_status})"





class MosquitoEvent(Base):
    __tablename__ = "mosquito_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    count: Mapped[int] = mapped_column(Integer, default=0)
    device: Mapped["Device"] = relationship("Device", back_populates="mosquito_readings")
    individual_readings: Mapped[list["MosquitoIndividualReading"]] = relationship(
        "MosquitoIndividualReading", back_populates="batch", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"MosquitoBatch(id={self.id}, device_id={self.device_id})"
    


class MosquitoIndividualReading(Base):
    __tablename__ = "mosquito_individual_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("mosquito_events.id"))
    detection_timestamp: Mapped[datetime] = mapped_column(DateTime)
    species: Mapped[str] = mapped_column(String(250), nullable=True)
    genus: Mapped[str] = mapped_column(String(250), nullable=True)
    age_group: Mapped[str] = mapped_column(String(50))
    sex: Mapped[str] = mapped_column(String(50))
    batch: Mapped["MosquitoEvent"] = relationship("MosquitoEvent", back_populates="individual_readings")

    def __repr__(self):
        return f"MosquitoEvent(id={self.id}, species={self.species})"
