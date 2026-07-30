from pydantic import BaseModel, EmailStr,Field,field_validator,ConfigDict,computed_field
from typing import Union,Optional
from datetime import datetime, timezone, timedelta
import uuid
from app.authentication.schema import UserResponse


# A device is considered active if it has reported activity within this window.
ACTIVE_WINDOW_HOURS = 24







 

class DeviceBase(BaseModel):
    name: str = Field(...,min_length=2, max_length=100, description="Name of the device")
    # Position and its derived labels are optional: the device fills them in
    # from its own readings, and reverse geocoding derives region/community.
    #
    # Deliberately NO ge/le bounds here. DeviceResponse inherits this model, and
    # a range check on an OUTPUT field turns any historically out-of-range row
    # into a 500 for the whole list endpoint. Bounds belong on the input
    # schemas (DeviceCreate/DeviceUpdate) only.
    longitude: Optional[float] = Field(None, description="Longitude of the device location")
    latitude: Optional[float] = Field(None, description="Latitude of the device location")
    region: Optional[str] = Field(None, max_length=100, description="Region, derived from the reported coordinates")
    community: Optional[str] = Field(None, max_length=150, description="Community/locality, derived from the reported coordinates")
    device_uuid: Optional[str] = Field(None, description="Unique UUID for the device")
    description: Optional[str] = Field(None, max_length=255, description="Description of the device")
    gmap_link: Optional[str] = Field(None, max_length=255, description="Google Maps link for the device location")
    






class DeviceCreate(DeviceBase):
    # validate_default=True is essential: Pydantic skips validators for fields
    # that are absent from the payload, so without it a client that OMITS
    # device_uuid gets None — and a device with no UUID can never be matched to
    # an MQTT topic or an ingest URL.
    device_uuid: Optional[str] = Field(
        None, validate_default=True, description="Unique UUID for the device (generated when omitted)"
    )
    cluster_id: Optional[int] = Field(None, description="ID of the device cluster this device belongs to")
    # Range checks apply to input only — see the note on DeviceBase.
    longitude: Optional[float] = Field(None, ge=-180, le=180, description="Longitude of the device location")
    latitude: Optional[float] = Field(None, ge=-90, le=90, description="Latitude of the device location")

    @field_validator("device_uuid", mode="before")
    @classmethod
    def generate_uuid(cls, v):
        if isinstance(v, str):
            v = v.strip()
        return v or str(uuid.uuid4())
    

class DeviceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=100, description="Name of the device")
    longitude: Optional[float] = Field(None, ge=-180, le=180, description="Longitude of the device location")
    latitude: Optional[float] = Field(None, ge=-90, le=90, description="Latitude of the device location")
    region: Optional[str] = Field(None, max_length=100, description="Region (normally derived from reported coordinates)")
    community: Optional[str] = Field(None, max_length=150, description="Community (normally derived from reported coordinates)")
    description: Optional[str] = Field(None, max_length=255, description="Description of the device")
    gmap_link: Optional[str] = Field(None, max_length=255, description="Google Maps link for the device location")
    device_uuid: Optional[str] = Field(None, description="Unique UUID for the device")
    cluster_id: Optional[int] = Field(None, description="ID of the device cluster this device belongs to")





class DeviceClusterCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100, description="Name of the device cluster")
    description: Optional[str] = Field(None, max_length=255, description="Description of the device cluster")
    public: bool = Field(False, description="Whether the device cluster is publicly visible")
    cluster_admins: Optional[list[int]] = Field(None, description="List of user IDs administering the cluster")
    users: Optional[list[int]] = Field(None, description="List of user IDs to assign to the cluster as members")



class DeviceClusterUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=100, description="Name of the device cluster")
    description: Optional[str] = Field(None, max_length=255, description="Description of the device cluster")
    public: Optional[bool] = Field(None, description="Whether the device cluster is publicly visible")
    cluster_admins: Optional[list[int]] = Field(None, description="List of user IDs administering the cluster")
    users: Optional[list[int]] = Field(None, description="List of user IDs to assign to the cluster as members (replaces the current member list)")

class SensorDataPayload(BaseModel):
    timestamp: datetime
    temp_external: float
    temp_internal: float
    humidity_external: float
    humidity_internal: float
    pressure_internal: float
    pressure_external: float
    external_light: float = 0.0
    battery: float
    trap_status: Optional[bool] = False
    # Optional GPS fix. When present it updates the device's stored position and
    # triggers reverse geocoding of region/community. Accepts decimal degrees or
    # scaled integers (e.g. microdegrees) — see utils.coordinates.
    latitude: Optional[float] = Field(None, description="Device latitude at the time of the reading")
    longitude: Optional[float] = Field(None, description="Device longitude at the time of the reading")



class SensorDataResponse(BaseModel):
    id: int
    device_id: int
    timestamp: datetime
    temp_external: Optional[float] = Field(None, alias="external_temperature")
    temp_internal: Optional[float] = Field(None, alias="internal_temperature")
    humidity_external: Optional[float] = Field(None, alias="external_humidity")
    humidity_internal: Optional[float] = Field(None, alias="internal_humidity")
    pressure_internal: Optional[float] = Field(None, alias="internal_pressure")
    external_pressure: Optional[float] = Field(None, alias="external_pressure")
    external_light: Optional[float] = Field(None)
    battery: Optional[float] = Field(None, alias="battery_voltage")
    trap_status: Optional[bool] = False

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class DeviceResponse(DeviceBase):
    id: int = Field(...,description="ID of the device")
    last_activity: datetime = Field(...,description="Last activity timestamp of the device")
    created_at: datetime = Field(...,description="Created at timestamp of the device")
    updated_at: datetime = Field(...,description="Updated at timestamp of the device")
    total_mosquito_count: int = Field(...,description="Total mosquito count recorded by the device")
    cluster_id: Optional[int] = Field(None, description="ID of the device cluster this device belongs to")
    location_updated_at: Optional[datetime] = Field(None, description="When the device last reported a new position")
    latest_reading: Optional[SensorDataResponse] = Field(None, description="Latest sensor reading from the device")

    @computed_field(
        description=f"Active if the device reported activity within the last {ACTIVE_WINDOW_HOURS}h; inactive if it never reported or has been silent longer.",
    )
    @property
    def is_active(self) -> bool:
        if not self.last_activity:
            return False
        last = self.last_activity
        now = datetime.now(timezone.utc)
        # last_activity may be stored timezone-naive; compare on the same basis.
        if last.tzinfo is None:
            now = now.replace(tzinfo=None)
        return (now - last) <= timedelta(hours=ACTIVE_WINDOW_HOURS)

    model_config = ConfigDict(from_attributes=True)





class DeviceClusterResponse(BaseModel):
    id: int = Field(..., description="ID of the device cluster")
    cluster_uuid: str = Field(..., description="Unique UUID for the device cluster")
    name: str = Field(..., description="Name of the device cluster")
    description: Optional[str] = Field(None, description="Description of the device cluster")
    public: bool = Field(..., description="Whether the device cluster is public")
    created_at: datetime = Field(..., description="Created at timestamp of the device cluster")
    updated_at: datetime = Field(..., description="Updated at timestamp of the device cluster")
    devices: list[DeviceResponse] = Field(default=[], description="List of devices in the cluster")
    admins: list[UserResponse] = Field(
        default=[],
        description="List of users administering the cluster",
        alias="cluster_admins",
        serialization_alias="admins",
    )
    users: list[UserResponse] = Field(
        default=[],
        description="List of users assigned to the cluster as members",
    )

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
 



class MosquitoIndividualPayload(BaseModel):
    detection_timestamp: datetime
    species: Optional[str] = None
    genus: Optional[str] = None
    age_group: Optional[str] = None
    sex: Optional[str] = None




class MosquitoEventPayload(BaseModel):
    timestamp: datetime
    mosquito_reading: MosquitoIndividualPayload = Field(..., description="The mosquito reading for this event")
    # Same optional GPS fix as SensorDataPayload — some firmware attaches the
    # position to detection events rather than to sensor frames.
    latitude: Optional[float] = Field(None, description="Device latitude at the time of the event")
    longitude: Optional[float] = Field(None, description="Device longitude at the time of the event")




class MosquitoIndividualResponse(MosquitoIndividualPayload):
    id: int = Field(..., description="ID of the individual mosquito reading")
    batch_id: int = Field(..., description="ID of the mosquito event batch this reading belongs to")
    device_uuid: Optional[str] = None 


    model_config = ConfigDict(from_attributes=True)


class MosquitoEventResponse(BaseModel):
    id: int = Field(..., description="ID of the mosquito event batch")
    device_id: int = Field(..., description="ID of the device")
    timestamp: datetime = Field(..., description="When the event batch was recorded")
    count: int = Field(..., description="Number of mosquitoes detected in this event")
    mosquito_reading: Optional[MosquitoIndividualResponse] = Field(
        default=None,
        description="The single mosquito reading for this event",
    )

    model_config = ConfigDict(from_attributes=True)








DeviceClusterResponse.model_rebuild()
