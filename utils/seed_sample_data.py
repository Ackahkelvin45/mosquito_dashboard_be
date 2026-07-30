"""Fill the database with realistic sample telemetry so the dashboard has data.

Generates ~13 months of sensor readings and mosquito detections for a fleet of
traps spread across Ghana, shaped so every chart window (year / month / week /
day / hour) has something sensible in it:

  * seasonal mosquito abundance following Ghana's two rainy seasons
  * nightly biting peaks (Anopheles) and dawn/dusk peaks (Aedes)
  * diurnal temperature/humidity/light cycles with harmattan and rainy periods
  * solar battery charge/discharge cycles, with the odd flat trap
  * species/genus/sex/age mix weighted the way a real catch looks

    uv run python -m utils.seed_sample_data            # add data (refuses if already seeded)
    uv run python -m utils.seed_sample_data --reset    # wipe telemetry first, then seed
    uv run python -m utils.seed_sample_data --days 90  # shorter history (faster)

Devices, clusters and users are never deleted — only the two telemetry tables
are touched, and only with --reset.
"""
import argparse
import math
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, insert, select

from app.core.database import SessionLocal
from app.device.enums import Status
from app.device.models import (
    Device,
    DeviceCluster,
    MosquitoEvent,
    MosquitoIndividualReading,
    SensorDeviceReading,
)
from app.authentication.models import User, ResearcherRequest  # noqa: F401  (mapper registry)

# Deterministic output, so re-seeding gives the same dashboard.
RNG = random.Random(20260730)

# Existing rows are only ~11 test records; anything above this means the
# database has already been seeded and re-running would double every chart.
ALREADY_SEEDED_THRESHOLD = 1_000

# ── The fleet ────────────────────────────────────────────────────────────────
# (uuid, name, region, community, lat, lon, cluster, abundance multiplier)
# The multiplier captures how mosquito-heavy a site is: coastal/forest wetlands
# swarm, the northern savannah is drier outside the single rainy season.
FLEET = [
    ("ESP32_001", "Teshie Trap A",   "Greater Accra Region", "Teshie Nungua Estates",  5.605948, -0.102990, "Greater Accra Surveillance", 1.15),
    ("ESP32_002", "East Legon Trap", "Greater Accra Region", "East Legon Extension",   5.647256, -0.168572, "Greater Accra Surveillance", 0.85),
    ("ESP32_003", "Ashaiman Trap",   "Greater Accra Region", "Ashaiman Lebanon",       5.694400, -0.033100, "Greater Accra Surveillance", 1.30),
    ("ESP32_004", "Ayigya Trap",     "Ashanti Region",       "Ayigya, Kumasi",         6.683600, -1.558600, "Ashanti Field Network",      1.20),
    ("ESP32_005", "Ejisu Trap",      "Ashanti Region",       "Ejisu",                  6.739200, -1.362800, "Ashanti Field Network",      1.00),
    ("ESP32_006", "Cape Coast Trap", "Central Region",       "Pedu, Cape Coast",       5.131500, -1.279500, "Coastal Belt Study",         1.10),
    ("ESP32_007", "Takoradi Trap",   "Western Region",       "Effiakuma, Takoradi",    4.901600, -1.783100, "Coastal Belt Study",         1.25),
    ("ESP32_008", "Ho Trap",         "Volta Region",         "Bankoe, Ho",             6.600800,  0.471300, "Volta Basin Study",          0.95),
    ("ESP32_009", "Tamale Trap",     "Northern Region",      "Sagnarigu, Tamale",      9.400800, -0.839300, "Northern Belt Study",        0.70),
    ("ESP32_010", "Bolgatanga Trap", "Upper East Region",    "Bolgatanga",            10.785600, -0.851400, "Northern Belt Study",        0.60),
]

CLUSTERS = {
    "Greater Accra Surveillance": "Urban coastal traps across the Accra metropolitan area.",
    "Ashanti Field Network":      "Forest-zone traps around Kumasi and its peri-urban belt.",
    "Coastal Belt Study":         "Central and Western coastal surveillance sites.",
    "Volta Basin Study":          "Traps along the Volta basin and its irrigated farmland.",
    "Northern Belt Study":        "Savannah-zone traps in the Northern and Upper East regions.",
}

# Species weighted by how often they turn up in a Ghanaian catch.
SPECIES = [
    ("Anopheles", "Anopheles gambiae",          0.30),
    ("Anopheles", "Anopheles funestus",         0.12),
    ("Anopheles", "Anopheles arabiensis",       0.06),
    ("Culex",     "Culex quinquefasciatus",     0.24),
    ("Culex",     "Culex tritaeniorhynchus",    0.05),
    ("Aedes",     "Aedes aegypti",              0.13),
    ("Aedes",     "Aedes albopictus",           0.06),
    ("Mansonia",  "Mansonia africana",          0.04),
]
_SPECIES_WEIGHTS = [w for _, _, w in SPECIES]

# Anopheles bite deep at night; Aedes at dawn and dusk; Culex through the
# evening. Index = hour of day, value = relative catch rate.
_NIGHT_BITERS = {"Anopheles", "Culex"}


def _hour_weight(genus: str, hour: int) -> float:
    if genus in _NIGHT_BITERS:
        # Peak around 01:00, trough at midday.
        return 0.12 + 0.88 * (0.5 * (1 + math.cos((hour - 1) * math.pi / 12)))
    # Aedes / Mansonia: twin peaks near 06:00 and 18:00.
    dawn = math.exp(-((hour - 6) ** 2) / 6)
    dusk = math.exp(-((hour - 18) ** 2) / 6)
    return 0.08 + 0.92 * max(dawn, dusk)


def _season(day_of_year: int) -> float:
    """Relative mosquito abundance across Ghana's bimodal rainy seasons.

    Major rains peak around mid-June (day 165), minor rains around early
    October (day 280); the harmattan trough sits in January.
    """
    major = math.exp(-((day_of_year - 165) ** 2) / 2200)
    minor = 0.62 * math.exp(-((day_of_year - 280) ** 2) / 1100)
    return 0.18 + 1.25 * max(major, minor)


def _rain_factor(day_of_year: int) -> float:
    """0 in the dry harmattan, 1 at the height of the rains — drives humidity."""
    return min(1.0, _season(day_of_year) / 1.2)


def _environment(ts: datetime, lat: float) -> dict:
    """Plausible sensor readings for an instant at a given latitude.

    The north (higher latitude) runs hotter and drier than the coast.
    """
    doy = ts.timetuple().tm_yday
    hour = ts.hour + ts.minute / 60
    northness = (lat - 4.9) / 6.0  # 0 at the coast, ~1 in the Upper East
    rain = _rain_factor(doy)

    # Daily temperature cycle: coolest ~05:00, hottest ~15:00.
    daily = math.cos((hour - 15) * math.pi / 12)
    base_temp = 27.0 + 3.4 * northness - 3.2 * rain
    swing = 3.6 + 3.4 * northness * (1 - rain)
    external_temp = base_temp + swing * daily + RNG.gauss(0, 0.55)

    # The trap housing lags the outside air and runs warmer in the sun.
    solar = max(0.0, math.sin((hour - 6) * math.pi / 12))
    internal_temp = external_temp + 0.8 + 2.4 * solar + RNG.gauss(0, 0.35)

    # Humidity is the inverse of the daily heat, lifted by the rains.
    external_hum = 62 + 26 * rain - 14 * northness * (1 - rain) - 11 * daily + RNG.gauss(0, 2.6)
    external_hum = max(18.0, min(99.0, external_hum))
    internal_hum = max(15.0, min(99.0, external_hum - 4.5 + RNG.gauss(0, 1.4)))

    # Pressure: gentle semidiurnal tide plus weather noise, thinner up north.
    pressure_ext = (
        1011.5 - 1.1 * northness
        + 0.9 * math.cos((hour - 10) * math.pi / 6)
        + RNG.gauss(0, 0.7)
    )
    pressure_int = pressure_ext + RNG.gauss(0.4, 0.3)

    # Light: zero at night, bell-shaped through the day, dimmed by rain clouds.
    if 6 <= hour <= 18:
        light = 980 * math.sin((hour - 6) * math.pi / 12) * (1 - 0.55 * rain)
        light = max(0.0, light + RNG.gauss(0, 35))
    else:
        light = max(0.0, RNG.gauss(1.5, 1.2))

    return {
        "external_temperature": round(external_temp, 2),
        "internal_temperature": round(internal_temp, 2),
        "external_humidity": round(external_hum, 2),
        "internal_humidity": round(internal_hum, 2),
        "external_pressure": round(pressure_ext, 2),
        "internal_pressure": round(pressure_int, 2),
        "external_light": round(light, 1),
    }


def _battery(ts: datetime, day_index: int, dead_days: set[int]) -> float:
    """Solar trap: charges through the day, drains overnight."""
    if day_index in dead_days:
        return round(RNG.uniform(3.28, 3.42), 2)
    hour = ts.hour + ts.minute / 60
    # Full by late afternoon, lowest just before dawn.
    charge = 0.5 * (1 + math.cos((hour - 16) * math.pi / 12))
    volts = 3.62 + 0.55 * charge - 0.06 * math.sin(day_index / 29) + RNG.gauss(0, 0.02)
    return round(max(3.35, min(4.20, volts)), 2)


def _reading_times(start: datetime, end: datetime) -> list[datetime]:
    """Coarse history, fine detail near now — so the hour/day views stay dense
    without carrying minute resolution across a whole year."""
    times: list[datetime] = []

    fine_start = end - timedelta(hours=6)     # every 5 min
    medium_start = end - timedelta(days=3)    # every 10 min
    hourly_start = end - timedelta(days=30)   # every hour

    cursor = start
    while cursor < hourly_start:
        times.append(cursor)
        cursor += timedelta(hours=3)

    cursor = max(cursor, hourly_start)
    while cursor < medium_start:
        times.append(cursor)
        cursor += timedelta(hours=1)

    cursor = max(cursor, medium_start)
    while cursor < fine_start:
        times.append(cursor)
        cursor += timedelta(minutes=10)

    cursor = max(cursor, fine_start)
    while cursor <= end:
        times.append(cursor)
        cursor += timedelta(minutes=5)

    return times


def _ensure_clusters(session) -> dict[str, int]:
    """Create the sample clusters if they are missing; return name -> id."""
    ids: dict[str, int] = {}
    for name, description in CLUSTERS.items():
        cluster = session.execute(
            select(DeviceCluster).where(DeviceCluster.name == name)
        ).scalar_one_or_none()
        if cluster is None:
            cluster = DeviceCluster(
                name=name,
                description=description,
                public=True,
                status=Status.APPROVED,
            )
            session.add(cluster)
            session.flush()
        ids[name] = cluster.id
    return ids


def _ensure_devices(session, cluster_ids: dict[str, int], now: datetime) -> list[Device]:
    """Create any missing traps and refresh the location of the existing ones."""
    devices: list[Device] = []
    for uuid, name, region, community, lat, lon, cluster_name, _ in FLEET:
        device = session.execute(
            select(Device).where(Device.device_uuid == uuid)
        ).scalar_one_or_none()
        if device is None:
            device = Device(device_uuid=uuid, name=name)
            session.add(device)
        device.name = name
        device.description = f"Sample surveillance trap at {community}."
        device.latitude = lat
        device.longitude = lon
        device.region = region
        device.community = community
        device.location_updated_at = now
        device.gmap_link = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
        device.cluster_id = cluster_ids[cluster_name]
        session.flush()
        devices.append(device)
    return devices


def _wipe_telemetry(session) -> tuple[int, int]:
    readings = session.execute(select(func.count()).select_from(SensorDeviceReading)).scalar_one()
    events = session.execute(select(func.count()).select_from(MosquitoEvent)).scalar_one()
    # Individual readings cascade off the events, but delete explicitly so the
    # count is right even if a row was ever orphaned.
    session.execute(delete(MosquitoIndividualReading))
    session.execute(delete(MosquitoEvent))
    session.execute(delete(SensorDeviceReading))
    session.commit()
    return readings, events


def _bulk_insert(session, table, rows: list[dict], chunk: int = 5_000) -> None:
    for i in range(0, len(rows), chunk):
        session.execute(insert(table), rows[i : i + chunk])
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="delete all existing sensor readings and mosquito events first")
    parser.add_argument("--days", type=int, default=400,
                        help="how many days of history to generate (default: 400)")
    parser.add_argument("--intensity", type=float, default=1.0,
                        help="scales how many mosquitoes are caught (default: 1.0)")
    args = parser.parse_args()

    # Naive UTC, matching how the rest of the backend stores timestamps.
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    start = now - timedelta(days=args.days)

    with SessionLocal() as session:
        existing = session.execute(
            select(func.count()).select_from(SensorDeviceReading)
        ).scalar_one()

        if args.reset:
            readings, events = _wipe_telemetry(session)
            print(f"Cleared {readings:,} sensor readings and {events:,} mosquito events.")
        elif existing > ALREADY_SEEDED_THRESHOLD:
            raise SystemExit(
                f"Database already holds {existing:,} sensor readings — it looks seeded.\n"
                "Re-run with --reset to wipe the telemetry tables and regenerate."
            )

        cluster_ids = _ensure_clusters(session)
        devices = _ensure_devices(session, cluster_ids, now)
        session.commit()
        print(f"Fleet ready: {len(devices)} devices across {len(cluster_ids)} clusters.")

        reading_rows: list[dict] = []
        event_rows: list[dict] = []
        # Individual readings need their event's id, so hold the descriptive
        # half here and pair it up after the events are inserted.
        individual_rows: list[dict] = []
        totals: dict[int, int] = {}

        for device, spec in zip(devices, FLEET):
            abundance = spec[7]
            lat = spec[4]

            # A couple of multi-day outages per device, so the status and
            # battery charts have something to show besides a flat line.
            span_days = max(1, args.days)
            dead_days: set[int] = set()
            for _ in range(RNG.randint(1, 3)):
                begin = RNG.randint(0, span_days - 1)
                dead_days.update(range(begin, min(span_days, begin + RNG.randint(2, 5))))
            # Never let the outage reach today — the trap must look alive now.
            dead_days.discard(span_days - 1)
            dead_days.discard(span_days - 2)

            # ── Sensor readings ──────────────────────────────────────────────
            for ts in _reading_times(start, now):
                day_index = (ts - start).days
                if day_index in dead_days:
                    continue  # trap offline: it reports nothing at all
                row = {
                    "device_id": device.id,
                    "timestamp": ts,
                    "battery_voltage": _battery(ts, day_index, dead_days),
                    # Traps run at night and are serviced during the day; the
                    # odd daytime reading stays on after a late shutdown.
                    "trap_status": (ts.hour >= 17 or ts.hour < 7) or RNG.random() < 0.05,
                    **_environment(ts, lat),
                }
                reading_rows.append(row)

            # ── Mosquito detections ──────────────────────────────────────────
            device_total = 0
            for day_index in range(span_days):
                day = start + timedelta(days=day_index)
                if day_index in dead_days:
                    continue
                doy = day.timetuple().tm_yday
                expected = 17.0 * abundance * _season(doy) * args.intensity
                caught = max(0, int(RNG.gauss(expected, expected * 0.28)))

                for _ in range(caught):
                    genus, species, _w = RNG.choices(SPECIES, weights=_SPECIES_WEIGHTS, k=1)[0]
                    # Draw an hour from that genus's biting profile.
                    hours = range(24)
                    hour = RNG.choices(
                        list(hours), weights=[_hour_weight(genus, h) for h in hours], k=1
                    )[0]
                    ts = day.replace(
                        hour=hour,
                        minute=RNG.randrange(60),
                        second=RNG.randrange(60),
                        microsecond=0,
                    )
                    if ts > now:
                        continue

                    # Traps attract host-seeking females, so the catch skews
                    # female; a small share of the catch is not yet mature.
                    sex = "female" if RNG.random() < 0.68 else "male"
                    age_group = "adult" if RNG.random() < 0.86 else "juvenile"

                    event_rows.append({
                        "device_id": device.id,
                        "timestamp": ts,
                        "count": 1,
                    })
                    individual_rows.append({
                        "detection_timestamp": ts,
                        "species": species,
                        "genus": genus,
                        "age_group": age_group,
                        "sex": sex,
                    })
                    device_total += 1

            totals[device.id] = device_total
            print(f"  {device.device_uuid}: {device_total:,} detections")

        print(f"Inserting {len(reading_rows):,} sensor readings …")
        _bulk_insert(session, SensorDeviceReading.__table__, reading_rows)

        print(f"Inserting {len(event_rows):,} mosquito events …")
        _bulk_insert(session, MosquitoEvent.__table__, event_rows)

        # Pair each individual reading with the event that was inserted for it.
        # Events were written in list order, so ordering by id restores it.
        event_ids = session.execute(
            select(MosquitoEvent.id).order_by(MosquitoEvent.id)
        ).scalars().all()
        if len(event_ids) != len(individual_rows):
            raise SystemExit(
                f"Event/reading mismatch ({len(event_ids)} vs {len(individual_rows)}) — "
                "the mosquito_events table was not empty. Re-run with --reset."
            )
        for batch_id, row in zip(event_ids, individual_rows):
            row["batch_id"] = batch_id

        print(f"Inserting {len(individual_rows):,} individual readings …")
        _bulk_insert(session, MosquitoIndividualReading.__table__, individual_rows)

        # Keep the denormalised counters on Device consistent with what we wrote.
        for device in devices:
            device.total_mosquito_count = totals.get(device.id, 0)
            last = session.execute(
                select(func.max(SensorDeviceReading.timestamp))
                .where(SensorDeviceReading.device_id == device.id)
            ).scalar_one_or_none()
            device.last_activity = last or now
        session.commit()

        print(
            "\nDone. "
            f"{len(reading_rows):,} readings, {len(event_rows):,} detections "
            f"across {len(devices)} devices, "
            f"{start.date()} → {now.date()}."
        )


if __name__ == "__main__":
    main()
