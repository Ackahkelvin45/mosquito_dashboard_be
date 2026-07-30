from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, and_, func
from typing import Optional, List
from app.crud.base import BaseRepository
from app.device.models import Device,DeviceCluster,SensorDeviceReading,MosquitoEvent,MosquitoIndividualReading
from app.device.schema import DeviceCreate, DeviceUpdate,SensorDataPayload,MosquitoEventPayload
from datetime import datetime, timezone
from fastapi import HTTPException
from utils.time_range import to_utc_naive
from app.service.device_location_service import apply_reported_position


def _as_filter_list(value) -> List[str]:
    """Normalize a filter that may be a single string or a list of strings."""
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    return [v.strip() for v in values if v and str(v).strip()]


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
            search: Optional[str] = None,
            min_mosquito_count: Optional[int] = None,
            max_mosquito_count: Optional[int] = None,
            latitude: Optional[float] = None,
            longitude: Optional[float] = None,
            cluster_id: "Optional[int | List[int]]" = None,
            created_after: Optional[datetime] = None,
            trap_status: Optional[bool] = None,
    ) -> List[Device]:
        query = self.session.query(Device)

        if search:
            like = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    Device.name.ilike(like),
                    func.coalesce(Device.region, "").ilike(like),
                    func.coalesce(Device.community, "").ilike(like),
                    func.coalesce(Device.device_uuid, "").ilike(like),
                )
            )
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
            # Accepts a single id or a multi-select list of ids.
            cluster_ids = [cluster_id] if isinstance(cluster_id, int) else [int(c) for c in cluster_id]
            if cluster_ids:
                query = query.filter(Device.cluster_id.in_(cluster_ids))
        if trap_status is not None:
            # "On/off" reflects the trap_status of each device's most recent sensor reading.
            latest_reading = (
                self.session.query(
                    SensorDeviceReading.device_id,
                    func.max(SensorDeviceReading.timestamp).label("max_ts"),
                )
                .group_by(SensorDeviceReading.device_id)
                .subquery()
            )
            query = (
                query.join(latest_reading, latest_reading.c.device_id == Device.id)
                .join(
                    SensorDeviceReading,
                    and_(
                        SensorDeviceReading.device_id == Device.id,
                        SensorDeviceReading.timestamp == latest_reading.c.max_ts,
                    ),
                )
                .filter(SensorDeviceReading.trap_status == trap_status)
            )

        return query.all()
    
    def get_mosquito_filter_options(self, allowed_cluster_ids: Optional[set] = None) -> dict:
        """Distinct values that power the historical-data filter dropdowns."""
        region_query = self.session.query(Device.region).filter(
            Device.region.isnot(None), Device.region != ""
        )
        if allowed_cluster_ids is not None:
            region_query = region_query.filter(Device.cluster_id.in_(allowed_cluster_ids))
        regions = sorted({row[0] for row in region_query.distinct()})

        reading_query = (
            self.session.query(MosquitoIndividualReading.genus, MosquitoIndividualReading.species)
            .join(MosquitoEvent, MosquitoIndividualReading.batch_id == MosquitoEvent.id)
            .join(Device, MosquitoEvent.device_id == Device.id)
        )
        if allowed_cluster_ids is not None:
            reading_query = reading_query.filter(Device.cluster_id.in_(allowed_cluster_ids))

        genus_values: set = set()
        species_values: set = set()
        for genus, species in reading_query.distinct():
            if genus:
                genus_values.add(genus)
            if species:
                species_values.add(species)

        return {
            "regions": regions,
            "genus": sorted(genus_values),
            "species": sorted(species_values),
        }

    def refresh_last_activity(self, device_id: int) -> None:
        device = self.get_by_id(device_id)
        if not device:
            raise ValueError("Device not found")
        device.last_activity = datetime.now(timezone.utc)
        self.session.commit()



    def create_sensor_reading(self, device: Device, payload: SensorDataPayload) -> SensorDeviceReading:
            # A GPS fix on the reading updates the device's position (and, via
            # reverse geocoding, its region/community).
            apply_reported_position(self.session, device, payload.latitude, payload.longitude)

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
        apply_reported_position(self.session, device, payload.latitude, payload.longitude)

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
        region: str | List[str] | None = None,
        device_uuids: List[str] | None = None,
        genus: str | List[str] | None = None,
        species: str | List[str] | None = None,
        allowed_cluster_ids: Optional[set] = None,
    ):
        if start_date is not None:
            start_date = to_utc_naive(start_date)
        if end_date is not None:
            end_date = to_utc_naive(end_date)
        if start_date is not None:
            query = query.filter(MosquitoEvent.timestamp >= start_date)
        if end_date is not None:
            query = query.filter(MosquitoEvent.timestamp <= end_date)

        # Cluster scope (requires Device to be joined — callers force the join
        # when allowed_cluster_ids is set).
        if allowed_cluster_ids is not None:
            query = query.filter(Device.cluster_id.in_(allowed_cluster_ids))

        # Multi-select filters: an event matches when it matches ANY of the
        # selected values (OR within a filter, AND across filters).
        regions = _as_filter_list(region)
        if regions:
            query = query.filter(or_(*[Device.region.ilike(f"%{r}%") for r in regions]))
        if device_uuids:
            query = query.filter(Device.device_uuid.in_(device_uuids))
        genus_values = _as_filter_list(genus)
        if genus_values:
            query = query.filter(or_(*[MosquitoIndividualReading.genus.ilike(f"%{g}%") for g in genus_values]))
        species_values = _as_filter_list(species)
        if species_values:
            query = query.filter(or_(*[MosquitoIndividualReading.species.ilike(f"%{s}%") for s in species_values]))

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
        region: str | List[str] | None = None,
        device_uuids: List[str] | None = None,
        genus: str | List[str] | None = None,
        species: str | List[str] | None = None,
        allowed_cluster_ids: Optional[set] = None,
    ) -> List[MosquitoEvent]:
        # Cluster scope needs the Device join, so force the join path when set.
        needs_join = bool(search or region or device_uuids or genus or species) or allowed_cluster_ids is not None

        if not needs_join:
            base_query = self.session.query(MosquitoEvent)
            base_query = self._apply_mosquito_event_filters(
                base_query,
                start_date=start_date,
                end_date=end_date,
            )
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
            region=region,
            device_uuids=device_uuids,
            genus=genus,
            species=species,
            allowed_cluster_ids=allowed_cluster_ids,
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
