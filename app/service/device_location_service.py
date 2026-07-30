"""Keep a device's stored position in sync with what it reports.

Called from both ingestion paths (MQTT and REST). Two separate concerns:

  1. The coordinates themselves — cheap, applied immediately and synchronously.
  2. region/community — needs a network round-trip to Nominatim, so it is
     resolved on a worker thread and must never block or fail ingestion.

Devices report every few seconds but move almost never, so geocoding only runs
when the device has actually moved a meaningful distance or when region /
community are still missing.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.device.models import Device
from app.notification.events import NotificationEvent, emit
from app.service.geocoding_service import reverse_geocode
from utils.coordinates import haversine_metres, normalize_lat_lon

logger = logging.getLogger(__name__)

# Below this, treat movement as GPS jitter rather than relocation. A stationary
# consumer GPS typically wanders 5-15 m between fixes.
MIN_MOVE_METRES = float(os.getenv("DEVICE_MIN_MOVE_METRES", "50"))

# When a device is stationary but still unlabelled (Nominatim was down, or the
# coordinates resolved to nothing), don't retry on every single reading — a
# device reporting every 30s would otherwise generate 2 lookups/minute forever.
GEOCODE_RETRY_SECONDS = float(os.getenv("DEVICE_GEOCODE_RETRY_SECONDS", "900"))

_attempt_lock = threading.Lock()
_last_geocode_attempt: dict[int, float] = {}


def _claim_geocode_slot(device_id: int, moved: bool) -> bool:
    """True if this caller may spend a geocode lookup on `device_id` now.

    A genuine move always earns a lookup; a retry for still-missing labels is
    rate-limited per device.
    """
    now = time.monotonic()
    with _attempt_lock:
        if moved:
            _last_geocode_attempt[device_id] = now
            return True
        last = _last_geocode_attempt.get(device_id)
        if last is not None and (now - last) < GEOCODE_RETRY_SECONDS:
            return False
        _last_geocode_attempt[device_id] = now
        return True


def apply_reported_position(
    session: Session,
    device: Device,
    latitude,
    longitude,
    *,
    geocode: bool = True,
) -> bool:
    """Update `device` from a reported position. Returns True if it moved.

    Does NOT commit — the caller owns the transaction (both ingest paths commit
    the reading and the position together). Geocoding is dispatched separately
    on its own session once this transaction has been committed.
    """
    lat, lon = normalize_lat_lon(latitude, longitude)
    if lat is None or lon is None:
        return False  # nothing usable reported; keep whatever we already had

    had_position = device.latitude is not None and device.longitude is not None
    moved = True
    if had_position:
        distance = haversine_metres(device.latitude, device.longitude, lat, lon)
        moved = distance >= MIN_MOVE_METRES

    needs_labels = not (device.region and device.community)
    if not moved and not needs_labels:
        return False  # same spot, already labelled — nothing to do

    if moved:
        device.latitude = lat
        device.longitude = lon
        device.location_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        logger.info("Device %s position updated to %s,%s", device.device_uuid, lat, lon)
        # Only a genuine relocation from a known position — the first fix a
        # device ever reports is not a "move".
        if had_position:
            emit(session, NotificationEvent.DEVICE_LOCATION_CHANGED,
                 device=device, distance_m=distance)

    if geocode and _claim_geocode_slot(device.id, moved):
        # Detached from this transaction on purpose: the HTTP call can take
        # seconds and must not hold a DB transaction or stall the caller.
        _dispatch_geocode(device.id, lat, lon)

    return moved


def _dispatch_geocode(device_id: int, latitude: float, longitude: float) -> None:
    thread = threading.Thread(
        target=_geocode_and_store,
        args=(device_id, latitude, longitude),
        name=f"geocode-device-{device_id}",
        daemon=True,
    )
    thread.start()


def _geocode_and_store(device_id: int, latitude: float, longitude: float) -> None:
    """Resolve region/community and persist them on a fresh session."""
    try:
        region, community = reverse_geocode(latitude, longitude)
        if not region and not community:
            return

        with SessionLocal() as session:
            device = session.query(Device).filter(Device.id == device_id).first()
            if not device:
                return
            # The device may have moved again while we were waiting on the
            # network; only apply if it is still at the position we looked up.
            if device.latitude is None or device.longitude is None:
                return
            if haversine_metres(device.latitude, device.longitude, latitude, longitude) >= MIN_MOVE_METRES:
                logger.info("Device %s moved during geocode — discarding stale result", device_id)
                return

            changed = False
            if region and device.region != region:
                device.region = region
                changed = True
            if community and device.community != community:
                device.community = community
                changed = True
            if changed:
                session.commit()
                logger.info(
                    "Device %s located: region=%r community=%r", device_id, region, community
                )
    except Exception:  # never let a background geocode take anything down
        logger.exception("Background geocode failed for device %s", device_id)


def resolve_location_now(
    latitude: float, longitude: float
) -> tuple[Optional[str], Optional[str]]:
    """Synchronous lookup, for admin/backfill use rather than ingestion."""
    lat, lon = normalize_lat_lon(latitude, longitude)
    if lat is None or lon is None:
        return None, None
    return reverse_geocode(lat, lon)
