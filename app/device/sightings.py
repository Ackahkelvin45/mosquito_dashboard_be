"""Unregistered-device sightings — upserted from the MQTT unknown-device path.

Like everything on the ingest hot path this must be failure-proof: a sighting
is nice-to-have telemetry, never worth losing throughput or crashing the
message loop over.
"""
import json
import logging
from datetime import datetime

from utils.coordinates import normalize_lat_lon

from app.device.models import UnregisteredDeviceSighting

logger = logging.getLogger(__name__)

# Keep the stored payload small — it exists only to prefill the registration
# form, not to archive traffic.
MAX_PAYLOAD_CHARS = 2000


def extract_raw_fix(data: dict) -> tuple:
    """Pull (lat, lon) out of a payload, tolerating common key spellings and a
    nested {"location"/"gps"/"position": {...}} wrapper. Returns raw values
    (not yet normalised); (None, None) when absent."""
    source = data
    for nested_key in ("location", "gps", "position"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            source = nested
            break
    lat = next((source.get(k) for k in ("latitude", "lat") if source.get(k) is not None), None)
    lon = next(
        (source.get(k) for k in ("longitude", "lon", "lng", "long") if source.get(k) is not None),
        None,
    )
    return lat, lon


def record_sighting(db, device_uuid: str, topic: str | None, data: dict | None) -> None:
    """Upsert the sighting row for a stray UUID. Never raises."""
    try:
        now = datetime.utcnow()

        payload = None
        if isinstance(data, dict):
            try:
                if len(json.dumps(data)) <= MAX_PAYLOAD_CHARS:
                    payload = data
            except (TypeError, ValueError):
                payload = None

        lat = lon = None
        if isinstance(data, dict):
            lat, lon = normalize_lat_lon(*extract_raw_fix(data))

        row = (
            db.query(UnregisteredDeviceSighting)
            .filter(UnregisteredDeviceSighting.device_uuid == device_uuid)
            .first()
        )
        if row is None:
            row = UnregisteredDeviceSighting(
                device_uuid=device_uuid, first_seen=now, message_count=0,
            )
            db.add(row)
        row.last_seen = now
        row.message_count += 1
        if topic:
            row.last_topic = str(topic)[:255]
        if payload is not None:
            row.last_payload = payload
        # Keep the last KNOWN fix — a payload without GPS must not erase one.
        if lat is not None and lon is not None:
            row.latitude, row.longitude = lat, lon
        db.commit()
    except Exception:
        logger.exception("Could not record unregistered-device sighting for %s", device_uuid)
        try:
            db.rollback()
        except Exception:
            pass


def clear_sighting(db, device_uuid: str | None) -> None:
    """Drop the sighting once a matching device gets registered. Never raises;
    the caller's commit already happened, this is its own transaction."""
    if not device_uuid:
        return
    try:
        deleted = (
            db.query(UnregisteredDeviceSighting)
            .filter(UnregisteredDeviceSighting.device_uuid == device_uuid)
            .delete(synchronize_session=False)
        )
        db.commit()
        if deleted:
            logger.info("Cleared unregistered sighting for now-registered %s", device_uuid)
    except Exception:
        logger.exception("Could not clear sighting for %s", device_uuid)
        try:
            db.rollback()
        except Exception:
            pass
