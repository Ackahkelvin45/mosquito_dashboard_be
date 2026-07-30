from app.core.database import Base
from sqlalchemy import Enum, Integer, String, DateTime, Boolean, Float, ForeignKey, Table, Column, select, and_
from sqlalchemy.orm import relationship, mapped_column, Mapped
from datetime import datetime
from app.authentication.enums import DeviceStatus
import uuid
from  .enums import Status



cluster_admins_table = Table(
    "cluster_admins",
    Base.metadata,
    Column("cluster_id", Integer, ForeignKey("device_clusters.id"), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
)



class DeviceCluster(Base):
    __tablename__ = "device_clusters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    cluster_uuid: Mapped[str] = mapped_column(String(100), unique=True, index=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255))
    public: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    devices: Mapped[list["Device"]] = relationship("Device", back_populates="cluster")
    status: Mapped[Status] = mapped_column(Enum(Status), default=Status.PENDING)
    researcher_request: Mapped["ResearcherRequest | None"] = relationship(
        "ResearcherRequest",
        back_populates="cluster",
        uselist=False,
    )
    cluster_admins: Mapped[list["User"]] = relationship(
        "User",
        secondary=cluster_admins_table,
        back_populates="clusters",
    )
    # Members: users whose users.cluster_id points here. Distinct from
    # cluster_admins (the M2M admin link) — a member just belongs to the
    # cluster and sees its data.
    users: Mapped[list["User"]] = relationship(
        "User",
        foreign_keys="User.cluster_id",
        back_populates="cluster",
    )

    def __repr__(self):
        return f"DeviceCluster(id={self.id}, name={self.name})"



class Device(Base):
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    device_uuid=mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    # Optional in the API schema, so it must be nullable in the table too —
    # otherwise creating a device without one is a 500, not a validation error.
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Position is nullable: a device can be registered before anyone knows where
    # it will sit, and the first reading that carries coordinates fills it in.
    # From then on the device's own reports are the source of truth.
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    # region/community are derived from the coordinates by reverse geocoding,
    # so they stay correct instead of drifting from a hand-typed guess.
    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    community: Mapped[str | None] = mapped_column(String(150), nullable=True)
    location_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gmap_link: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_activity: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # State marker for the offline detector job: set when the device is first
    # flagged offline, cleared (with a DEVICE_ONLINE notification) on recovery.
    offline_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    total_mosquito_count: Mapped[int] = mapped_column(Integer, default=0)
    device_reading:Mapped[list["SensorDeviceReading"]]=relationship("SensorDeviceReading", back_populates="device", cascade="all, delete-orphan")
    mosquito_readings:Mapped[list["MosquitoEvent"]]=relationship("MosquitoEvent", back_populates="device", cascade="all, delete-orphan")
    cluster_id: Mapped[int] = mapped_column(Integer, ForeignKey("device_clusters.id"), nullable=True)
    cluster: Mapped["DeviceCluster"] = relationship("DeviceCluster", back_populates="devices")
    # Pinned to exactly one row via a correlated LIMIT 1 subquery. A plain
    # order_by + uselist=False loads EVERY reading for the device on each
    # access, which grows unbounded as devices stream data.
    latest_reading: Mapped["SensorDeviceReading | None"] = relationship(
        "SensorDeviceReading",
        primaryjoin=lambda: and_(
            Device.id == SensorDeviceReading.device_id,
            SensorDeviceReading.id
            == select(SensorDeviceReading.id)
            .where(SensorDeviceReading.device_id == Device.id)
            .order_by(
                SensorDeviceReading.timestamp.desc(),
                SensorDeviceReading.id.desc(),
            )
            .limit(1)
            .correlate(Device)
            .scalar_subquery(),
        ),
        uselist=False,
        viewonly=True,
        overlaps="device_reading",
    )

 
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
    external_temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    internal_temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    external_humidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    internal_humidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    internal_pressure: Mapped[float | None] = mapped_column(Float, nullable=True)
    external_pressure: Mapped[float | None] = mapped_column(Float, nullable=True)
    external_light: Mapped[float | None] = mapped_column(Float, nullable=True)
    battery_voltage: Mapped[float | None] = mapped_column(Float, nullable=True)
    trap_status: Mapped[bool] = mapped_column(Boolean, default=False)
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
    mosquito_reading: Mapped["MosquitoIndividualReading | None"] = relationship(
        "MosquitoIndividualReading",
        back_populates="batch",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"MosquitoBatch(id={self.id}, device_id={self.device_id})"
    


class MosquitoIndividualReading(Base):
    __tablename__ = "mosquito_individual_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # One-to-one: each MosquitoEvent should have exactly one corresponding MosquitoIndividualReading.
    batch_id: Mapped[int] = mapped_column(ForeignKey("mosquito_events.id"), unique=True, index=True)
    detection_timestamp: Mapped[datetime] = mapped_column(DateTime)
    species: Mapped[str] = mapped_column(String(250), nullable=True)
    genus: Mapped[str] = mapped_column(String(250), nullable=True)
    age_group: Mapped[str] = mapped_column(String(50))
    sex: Mapped[str] = mapped_column(String(50))
    batch: Mapped["MosquitoEvent"] = relationship("MosquitoEvent", back_populates="mosquito_reading")

    def __repr__(self):
        return f"MosquitoEvent(id={self.id}, species={self.species})"
    
    @property
    def device_uuid(self):
        if self.batch and self.batch.device:
            return self.batch.device.device_uuid
        return None
