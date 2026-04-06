from pydantic import BaseModel, EmailStr,Field,field_validator,ConfigDict
from typing import Union,Optional
from datetime import datetime
import uuid
from app.authentication.schema import UserResponse







 

class DeviceBase(BaseModel):
    name: str = Field(...,min_length=2, max_length=100, description="Name of the device")
    longitude: float = Field(..., description="Longitude of the device location")
    latitude: float = Field(..., description="Latitude of the device location")
    region: str = Field(...,min_length=2, max_length=100, description="Region of the device location")
    device_uuid: Optional[str] = Field(None, description="Unique UUID for the device")
    description: Optional[str] = Field(None, max_length=255, description="Description of the device")
    gmap_link: Optional[str] = Field(None, max_length=255, description="Google Maps link for the device location")
    






class DeviceCreate(DeviceBase):
    device_uuid: Optional[str] = Field(None, description="Unique UUID for the device")
    cluster_id: Optional[int] = Field(None, description="ID of the device cluster this device belongs to")

    @field_validator("device_uuid", mode="before")
    @classmethod
    def generate_uuid(cls, v):
        return v or str(uuid.uuid4())
    

class DeviceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=100, description="Name of the device")
    longitude: Optional[float] = Field(None, description="Longitude of the device location")
    latitude: Optional[float] = Field(None, description="Latitude of the device location")
    region: Optional[str] = Field(None, min_length=2, max_length=100, description="Region of the device location")
    description: Optional[str] = Field(None, max_length=255, description="Description of the device")
    gmap_link: Optional[str] = Field(None, max_length=255, description="Google Maps link for the device location")
    device_uuid: Optional[str] = Field(None, description="Unique UUID for the device")
    cluster_id: Optional[int] = Field(None, description="ID of the device cluster this device belongs to")





class DeviceClusterCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100, description="Name of the device cluster")
    description: Optional[str] = Field(None, max_length=255, description="Description of the device cluster")
    password: str = Field(..., min_length=6, description="Password for the device cluster")
    public: bool = Field(False, description="Whether the device cluster is publicly visible")
    cluster_admins: Optional[list[int]] = Field(None, description="List of user IDs administering the cluster")


 
class DeviceClusterUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=100, description="Name of the device cluster")
    description: Optional[str] = Field(None, max_length=255, description="Description of the device cluster")
    password: Optional[str] = Field(None, min_length=6, description="Password for the device cluster")
    public: Optional[bool] = Field(None, description="Whether the device cluster is publicly visible")
    cluster_admins: Optional[list[int]] = Field(None, description="List of user IDs administering the cluster")

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
    latest_reading: Optional[SensorDataResponse] = Field(None, description="Latest sensor reading from the device")

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
 
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
 



class MosquitoIndividualPayload(BaseModel):
    detection_timestamp: datetime
    species: Optional[str] = None
    genus: Optional[str] = None
    age_group: Optional[str] = None
    sex: Optional[str] = None




class MosquitoEventPayload(BaseModel):
    timestamp: datetime
    mosquito_data: list[MosquitoIndividualPayload]=Field(..., description="List of individual mosquito readings in the event")




class MosquitoIndividualResponse(MosquitoIndividualPayload):
    id: int = Field(..., description="ID of the individual mosquito reading")
    batch_id: int = Field(..., description="ID of the mosquito event batch this reading belongs to")

    model_config = ConfigDict(from_attributes=True)


class MosquitoEventResponse(BaseModel):
    id: int = Field(..., description="ID of the mosquito event batch")
    device_id: int = Field(..., description="ID of the device")
    timestamp: datetime = Field(..., description="When the event batch was recorded")
    count: int = Field(..., description="Number of mosquitoes detected in this event")
    individual_readings: list[MosquitoIndividualResponse] = Field(
        default=[], description="Individual mosquito detections in this event"
    )

    model_config = ConfigDict(from_attributes=True)








DeviceClusterResponse.model_rebuild()
