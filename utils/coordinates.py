"""Coordinate normalisation and distance helpers.

Devices in the field do not agree on units: some report decimal degrees, others
report scaled integers (microdegrees are common on ESP32 GPS builds). Device 5
in this database stores longitude as `-168572`, which is -0.168572° in
microdegrees. The frontend already compensates at render time; normalising on
ingest means the stored value is correct in the first place.
"""
from math import asin, cos, radians, sin, sqrt
from typing import Optional

MAX_LAT = 90.0
MAX_LON = 180.0

# Tried in order — the first that brings the value into range wins, matching
# the frontend's normalizeCoordinate so both agree on the same answer.
_DIVISORS = (1_000_000, 100_000, 10_000, 1_000)


def normalize_coordinate(value, max_abs: float) -> Optional[float]:
    """Coerce a reported coordinate into decimal degrees, or None if unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric != numeric or numeric in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if abs(numeric) <= max_abs:
        return numeric
    for divisor in _DIVISORS:
        scaled = numeric / divisor
        if abs(scaled) <= max_abs:
            return scaled
    return None


def normalize_lat_lon(latitude, longitude) -> tuple[Optional[float], Optional[float]]:
    """Normalise a pair. Returns (None, None) unless BOTH are usable — a half
    position would place the device somewhere it has never been."""
    lat = normalize_coordinate(latitude, MAX_LAT)
    lon = normalize_coordinate(longitude, MAX_LON)
    if lat is None or lon is None:
        return None, None
    # 0,0 ("null island") is what a GPS module reports before it gets a fix.
    if lat == 0.0 and lon == 0.0:
        return None, None
    return lat, lon


def haversine_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6_371_000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))
