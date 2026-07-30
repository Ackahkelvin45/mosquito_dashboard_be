"""Reverse geocoding via Nominatim (OpenStreetMap).

Turns a device's reported coordinates into a region and a community, so those
never have to be typed by hand (and can't drift out of sync with reality —
device 4 was manually labelled "Eastern Region" while sitting in Greater Accra).

Design constraints this respects:
  * Nominatim's usage policy: max 1 request/second, and a real User-Agent
    identifying the application. Both are enforced here, not left to callers.
  * Never break ingestion. Every failure path returns None; a geocode that
    times out must not cost us a sensor reading.
  * Devices report constantly but rarely move, so results are cached by rounded
    coordinate and callers only geocode on meaningful movement.
"""
import logging
import os
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse")
# Nominatim asks for a contact address in the User-Agent so they can reach you
# about excessive use. Override via env for production deployments.
USER_AGENT = os.getenv(
    "NOMINATIM_USER_AGENT",
    "MosquitoSurveillanceDashboard/1.0 (+https://mosquitosurveillancedashboard.website)",
)
REQUEST_TIMEOUT_SECONDS = float(os.getenv("NOMINATIM_TIMEOUT", "6"))
MIN_INTERVAL_SECONDS = float(os.getenv("NOMINATIM_MIN_INTERVAL", "1.0"))

# ~11 m at the equator: fine enough to distinguish communities, coarse enough
# that GPS jitter keeps hitting the same cache entry.
_CACHE_PRECISION = 4
_CACHE_MAX_ENTRIES = 2_000

_cache: dict[tuple, tuple[Optional[str], Optional[str]]] = {}
_cache_lock = threading.Lock()
_rate_lock = threading.Lock()
_last_request_at = 0.0

# Nominatim's address keys, most specific first. The first hit becomes the
# community; "region" prefers the administrative level a person would name.
_COMMUNITY_KEYS = (
    "neighbourhood", "suburb", "quarter", "hamlet", "village",
    "town", "city_district", "municipality", "city",
)
_REGION_KEYS = ("state", "region", "province", "county", "state_district")


def _throttle() -> None:
    """Serialise callers to at most one request per MIN_INTERVAL_SECONDS."""
    global _last_request_at
    with _rate_lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _pick(address: dict, keys) -> Optional[str]:
    for key in keys:
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def reverse_geocode(latitude: float, longitude: float) -> tuple[Optional[str], Optional[str]]:
    """Return (region, community). Either may be None; never raises.

    Blocking — call it off the request/event loop (see location_service).
    """
    key = (round(latitude, _CACHE_PRECISION), round(longitude, _CACHE_PRECISION))
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    region = community = None
    try:
        _throttle()
        response = requests.get(
            NOMINATIM_URL,
            params={
                "lat": latitude,
                "lon": longitude,
                "format": "jsonv2",
                # zoom 14 ≈ suburb/village level — the granularity we want.
                "zoom": 14,
                "addressdetails": 1,
            },
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            logger.warning(
                "Nominatim returned %s for %s,%s — leaving region/community unchanged",
                response.status_code, latitude, longitude,
            )
            return None, None

        address = (response.json() or {}).get("address") or {}
        region = _pick(address, _REGION_KEYS)
        community = _pick(address, _COMMUNITY_KEYS)

        # Don't let community merely repeat the region.
        if community and region and community.casefold() == region.casefold():
            community = None

    except requests.RequestException as exc:
        logger.warning("Reverse geocode failed for %s,%s: %s", latitude, longitude, exc)
        return None, None
    except (ValueError, TypeError) as exc:  # malformed JSON / unexpected shape
        logger.warning("Could not parse geocode response for %s,%s: %s", latitude, longitude, exc)
        return None, None

    if region or community:
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX_ENTRIES:
                _cache.clear()
            _cache[key] = (region, community)
    return region, community
