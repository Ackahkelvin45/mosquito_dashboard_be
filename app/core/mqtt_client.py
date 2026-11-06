import json
import logging
from datetime import datetime, timezone
from fastapi_mqtt import FastMQTT, MQTTConfig
from app.device.models import (
    DEVICE_SENT_SENSOR_READING, Device, SensorDeviceReading, MosquitoEvent, MosquitoIndividualReading,
)
from app.device.sightings import extract_raw_fix, record_sighting
from app.core.database import SessionLocal
from app.core.config import settings
from app.device.imports import MOSQUITO_STRINGS, RowError, parse_bounded, parse_float
from app.monitoring import recorder
from app.notification.events import NotificationEvent, emit
from app.service.device_location_service import apply_reported_position
from utils.trap_status import parse_trap_status

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BROKER = settings.MQTT_BROKER
PORT = settings.MQTT_PORT
TOPIC_SENSOR_DATA = settings.TOPIC_SENSOR_DATA
TOPIC_MOSQUITO_COUNT = settings.TOPIC_MOSQUITO_COUNT
# Devices in Test mode publish to the same topics with a "_test" suffix
# (MQTT_Schema_Reference.pdf). An MQTT "+" wildcard only matches a whole
# segment, so "mosquito_dashboard/+/sensor_data" never matches
# ".../sensor_data_test" — the test variants need their own subscription.
TOPIC_SENSOR_DATA_TEST = f"{TOPIC_SENSOR_DATA}_test"
TOPIC_MOSQUITO_COUNT_TEST = f"{TOPIC_MOSQUITO_COUNT}_test"
CLIENT_ID = settings.MQTT_CLIENT_ID

mqtt_config = MQTTConfig(
    host=BROKER,
    port=PORT,
    reconnect_retries=-1,
    reconnect_delay=5,
)

mqtt = FastMQTT(config=mqtt_config, client_id=CLIENT_ID)


def _device_time(value) -> datetime | None:
    """Parse sensor_data/mosquito_data 'timestamp' fields to naive UTC, or
    None when missing/unparseable.

    Per MQTT_Schema_Reference.pdf the real device always sends UTC epoch
    SECONDS as an integer — that's the authoritative format. ISO strings are
    also accepted (REST callers, older test fixtures, already-parsed values);
    a tz-aware value is converted to UTC.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            logger.warning(f"Could not parse timestamp '{value}', using utcnow")
            return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.utcfromtimestamp(value)
        except (ValueError, OSError, OverflowError):
            logger.warning(f"Could not parse epoch timestamp '{value}', using utcnow")
    return None


def _logged_time(source: dict, key: str, invalid: list) -> datetime | None:
    """_device_time(source[key]); when missing or unparseable the caller falls
    back to server time (the columns are NOT NULL), so record that in
    `invalid` — the stored time is not the device's."""
    ts = _device_time(source.get(key))
    if ts is None:
        invalid.append(f"{key}={source.get(key)!r}"[:500])
    return ts


def _received_at(device_ts: datetime | None) -> datetime | None:
    """Server receive time — only when the device sent its own timestamp.
    A defaulted timestamp is server time too, so its delay would be a fake ~0."""
    return datetime.utcnow() if device_ts is not None else None


# ── Field checks ─────────────────────────────────────────────────────────────
# A value that can't be stored as sent — one the CSV import would reject, or
# one the DB would refuse (losing the whole message) — is stored blank, never
# clamped or truncated, and its raw form logged as an "invalid_value" error.
# Numbers and text use the import's own rules so every export re-imports.

def _checked(source: dict, key: str, invalid: list, parse):
    """parse(source[key]), or None (+ "key=repr" in `invalid`) when it raises.
    Missing/None is simply None, not an error."""
    value = source.get(key)
    if value is None:
        return None
    try:
        return parse(value)
    except (RowError, ValueError, TypeError):
        invalid.append(f"{key}={value!r}"[:500])
        return None


def _number(value):
    """Finite number (numeric strings too), as the import's parse_float."""
    return parse_float(str(value), "")


def _bounded(low: float, high: float, whole: bool = False):
    return lambda value: parse_bounded(str(value), "", low, high, whole=whole)


def _flag(value):
    """True/False (or 1/0) — anything else makes the Boolean column raise."""
    if value not in (True, False):
        raise ValueError
    return bool(value)


def _text(max_len: int):
    """A string that fits its column, without NUL — as the import requires."""
    def parse(value):
        if not isinstance(value, str) or "\x00" in value or len(value) > max_len:
            raise ValueError
        value.encode("utf-8")  # a lone surrogate ("\ud800") fails at commit otherwise
        return value
    return parse


def _strict_json(value):
    """JSON without NaN/Infinity, which Postgres json rejects."""
    json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")  # + lone surrogates
    return value


def _record_invalid(db, device: Device, invalid: list) -> None:
    """One "invalid_value" System Health row per blanked field (after the
    reading is committed; record_error is failure-proof and truncates)."""
    for detail in invalid:
        recorder.record_error(db, "invalid_value", device_uuid=device.device_uuid, detail=detail)


def _apply_position_from_payload(db, device: Device, data: dict) -> None:
    """Update the device's position if the payload carried a GPS fix.

    Key-spelling tolerance lives in sightings.extract_raw_fix, shared with the
    unregistered-device path so both parse coordinates identically.
    """
    lat, lon = extract_raw_fix(data)
    if lat is None or lon is None:
        return

    try:
        apply_reported_position(db, device, lat, lon)
    except Exception:
        # A bad GPS fix must never cost us the reading itself.
        logger.exception("Could not apply reported position for device %s", device.device_uuid)


def handle_sensor_data(db, device: Device, data: dict, is_test: bool = False):
    """
    Handles sensor_data topic payload (see MQTT_Schema_Reference.pdf):
    {
        "timestamp": 1786270740,
        "sensor_id": "AI4PEP_6825DD44B768",
        "temp_internal": 29.4,
        "temp_external": 27.8,
        "humidity_internal": 45.8,
        "humidity_external": 78.5,
        "pressure_internal": 1012.3,
        "pressure_external": 1010.2,
        "battery": 12.64043,       # voltage — profile-dependent range
        "battery_pct": 100,        # 0-100, already normalised for the profile
        "trap_status": "ON",       # exactly "ON" or "OFF"
        "esp1_link_alive": false,  # Listener-Unit link health
        "latitude": 14.2582913,    # optional GPS fix (null when no fix) —
        "longitude": 121.0678318   # updates the device's position/region
    }
    rainfall is currently always null (rain gauge not yet deployed on any
    unit) — intentionally not persisted; add a column once that hardware
    ships and the schema documents real units/range.

    is_test marks readings that arrived on the "_test"-suffixed topic (a
    device explicitly put into Test mode). They're stored so a technician
    can confirm connectivity, but never feed notifications or real device
    state (trap-flip detection, malfunction alerts, etc).
    """
    _apply_position_from_payload(db, device, data)

    # Trap-flip detection needs the state as it was BEFORE this reading
    # lands, scoped to real (non-test) readings only — a technician cycling
    # the trap in Test mode must never fire (or suppress) a live alert.
    previous_trap_on = False
    if not is_test:
        previous = (
            db.query(SensorDeviceReading.trap_status)
            .filter(SensorDeviceReading.device_id == device.id,
                    SensorDeviceReading.is_test.is_(False), DEVICE_SENT_SENSOR_READING)
            .order_by(SensorDeviceReading.timestamp.desc(), SensorDeviceReading.id.desc())
            .first()
        )
        previous_trap_on = bool(previous[0]) if previous else False

    invalid: list[str] = []
    device_ts = _logged_time(data, "timestamp", invalid)
    raw_trap = data.get("trap_status")
    if not (raw_trap in (True, False)
            or (isinstance(raw_trap, str) and raw_trap.strip().upper() in ("ON", "OFF"))):
        # NOT NULL column: parse_trap_status coerces (bool(value), so 2 → ON)
        # or defaults it to OFF — logged, so the guess is visible.
        invalid.append(f"trap_status={raw_trap!r}"[:500])
    number = lambda key: _checked(data, key, invalid, _number)  # noqa: E731
    reading = SensorDeviceReading(
        device_id=device.id,
        timestamp=device_ts or datetime.utcnow(),
        external_temperature=number("temp_external"),
        internal_temperature=number("temp_internal"),
        external_humidity=number("humidity_external"),
        internal_humidity=number("humidity_internal"),
        internal_pressure=number("pressure_internal"),
        external_pressure=number("pressure_external"),
        external_light=number("external_light"),
        battery_voltage=number("battery"),
        battery_pct=_checked(data, "battery_pct", invalid, _bounded(0, 100, whole=True)),
        trap_status=parse_trap_status(raw_trap, default=False),
        esp1_link_alive=_checked(data, "esp1_link_alive", invalid, _flag),
        is_test=is_test,
        received_at=_received_at(device_ts),
    )
    db.add(reading)
    device.last_activity = datetime.utcnow()
    # Liveness heartbeat: only sensor_data (periodic by contract) counts.
    # Test-mode readings count too — a device in Test mode is reachable, and
    # liveness is a separate concern from data validity.
    device.last_sensor_data_at = datetime.utcnow()
    db.commit()
    logger.info(
        f"Sensor reading saved for device {device.device_uuid}"
        + (" [TEST — no notifications]" if is_test else "")
    )
    _record_invalid(db, device, invalid)

    all_fields_empty = all(v is None for v in (
        reading.external_temperature, reading.internal_temperature,
        reading.external_humidity, reading.internal_humidity,
        reading.internal_pressure, reading.external_pressure,
        reading.external_light, reading.battery_voltage,
    ))
    if all_fields_empty:
        # Every field came back NULL — almost always a payload whose KEYS
        # don't match the schema (firmware variant), not a device with no
        # sensors. Keep the raw payload in the System Health error feed so
        # the offending JSON is diagnosable after the fact; registered
        # devices' payloads are not stored anywhere else. Captured for test
        # mode too — a mis-shaped payload matters either way.
        try:
            payload_snippet = json.dumps(data)[:500]
        except (TypeError, ValueError):
            payload_snippet = str(data)[:500]
        recorder.record_error(db, "empty_reading",
                              device_uuid=device.device_uuid,
                              detail=payload_snippet)

    if is_test:
        return

    emit(db, NotificationEvent.LOW_BATTERY, device=device,
         voltage=reading.battery_voltage, pct=reading.battery_pct)
    if reading.trap_status and not previous_trap_on:
        emit(db, NotificationEvent.TRAP_TRIGGERED, device=device)
    # External sensors reflect the environment; fall back to internal when absent.
    temperature = (
        reading.external_temperature
        if reading.external_temperature is not None
        else reading.internal_temperature
    )
    emit(db, NotificationEvent.EXTREME_TEMPERATURE, device=device, temperature=temperature)
    humidity = (
        reading.external_humidity
        if reading.external_humidity is not None
        else reading.internal_humidity
    )
    emit(db, NotificationEvent.EXTREME_HUMIDITY, device=device, humidity=humidity)
    if all_fields_empty:
        emit(db, NotificationEvent.SENSOR_MALFUNCTION, device=device,
             reason="all sensor fields were empty")


def handle_mosquito_event(db, device: Device, data: dict, is_test: bool = False):
    """
    Handles mosquito_data topic payload (see MQTT_Schema_Reference.pdf):
    {
        "timestamp": 1786270740,
        "sensor_id": "AI4PEP_6825DD44B768",
        "mosquito_data": [
            {
                "detection_timestamp": 1786270680,
                "species": "Aedes sp.",       # placeholder, genus + " sp."
                "genus": "aedes",              # the real model output
                "age_group": "",               # placeholder, always ""
                "sex": "female",
                "p_mosq": 0.87,
                "binary_decision": true,
                "taxon_probs": {"anopheles": 0, "aedes": 0.91, "culex": 0},
                "sex_probs": {"male": 0, "female": 0.85},
                "inference_ms": 215
            }
        ]
    }
    Currently always exactly one entry, but the schema is explicit that
    mosquito_data is an array and a future pipeline may report more than one
    detection per message — every entry here is processed, none dropped.
    species_probs is omitted: the schema defines it as always-zero
    placeholder values with no real model output behind it.

    is_test marks events from the "_test"-suffixed topic: stored for
    visibility, but never counted in device.total_mosquito_count and never
    trigger SPECIES_DETECTED/ACTIVITY_SURGE notifications.
    """
    mosquito_reading = data.get("mosquito_reading")
    if mosquito_reading is not None:
        readings = [mosquito_reading]
    else:
        mosquito_data = data.get("mosquito_data")
        if isinstance(mosquito_data, dict):
            readings = [mosquito_data]
        elif isinstance(mosquito_data, list):
            readings = [r for r in mosquito_data if isinstance(r, dict)]
        else:
            readings = []

    if not readings:
        logger.info(f"No mosquito reading in payload for device {device.device_uuid}")
        return

    _apply_position_from_payload(db, device, data)
    invalid: list[str] = []
    device_ts = _logged_time(data, "timestamp", invalid)
    outer_timestamp = device_ts or datetime.utcnow()

    saved = 0
    detected: list[dict] = []
    for mosquito_reading in readings:
        text = lambda key: _checked(mosquito_reading, key, invalid, _text(MOSQUITO_STRINGS[key]))  # noqa: E731
        event = MosquitoEvent(device_id=device.id, timestamp=outer_timestamp, count=1, is_test=is_test,
                             received_at=_received_at(device_ts))
        db.add(event)
        db.flush()

        individual = MosquitoIndividualReading(
            batch_id=event.id,
            detection_timestamp=_logged_time(mosquito_reading, "detection_timestamp", invalid) or datetime.utcnow(),
            species=text("species"),
            genus=text("genus"),
            # age_group/sex columns are NOT NULL; the schema guarantees the
            # keys (age_group is always "" today), but a missing or invalid
            # value must degrade to "" rather than blow up the whole commit.
            age_group=text("age_group") or "",
            sex=text("sex") or "",
            p_mosq=_checked(mosquito_reading, "p_mosq", invalid, _bounded(0, 1)),
            binary_decision=_checked(mosquito_reading, "binary_decision", invalid, _flag),
            taxon_probs=_checked(mosquito_reading, "taxon_probs", invalid, _strict_json),
            sex_probs=_checked(mosquito_reading, "sex_probs", invalid, _strict_json),
            # Not exported/imported; whole ms that fits the Integer column.
            inference_ms=_checked(mosquito_reading, "inference_ms", invalid, _bounded(0, 2**31 - 1, whole=True)),
        )
        db.add(individual)
        saved += 1

        # The stored (checked) values, emitted only after the commit: a failed
        # emit rolls the session back and must never cost the detection.
        detected.append({"species": individual.species, "genus": individual.genus,
                         "sex": individual.sex, "age_group": individual.age_group})

    device.last_activity = datetime.utcnow()
    if not is_test:
        device.total_mosquito_count = (device.total_mosquito_count or 0) + saved
    db.commit()
    _record_invalid(db, device, invalid)
    if is_test:
        logger.info(f"{saved} TEST mosquito event(s) saved for device {device.device_uuid} — not counted")
        return

    logger.info(f"{saved} mosquito event(s) saved for device {device.device_uuid}")

    for fields in detected:
        emit(db, NotificationEvent.SPECIES_DETECTED, device=device, **fields)
    emit(db, NotificationEvent.ACTIVITY_SURGE, device=device)


@mqtt.on_connect()
def on_connect(client, flags, rc, properties):
    logger.info(f"Connected to MQTT broker at {BROKER}:{PORT}")
    recorder.mark_connected()


@mqtt.subscribe(TOPIC_SENSOR_DATA, TOPIC_MOSQUITO_COUNT, TOPIC_SENSOR_DATA_TEST, TOPIC_MOSQUITO_COUNT_TEST)
async def on_message(client, topic, payload, qos, properties):
    topic_str = topic
    recorder.mark_message()

    try:
        data = json.loads(payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"Failed to parse message on topic {topic_str}: {e}")
        with SessionLocal() as db:
            emit(db, NotificationEvent.INVALID_PAYLOAD, topic=topic_str, error=str(e))
            recorder.record_error(db, "invalid_payload", topic=topic_str, detail=str(e))
        return

    # Extract device UUID + event type from topic: mosquito_dashboard/<device_uuid>/<event_type>
    parts = topic_str.split("/")
    if len(parts) < 3:
        logger.error(f"Malformed topic received: '{topic_str}' — expected format: mosquito_dashboard/<device_uuid>/<event_type>")
        with SessionLocal() as db:
            emit(db, NotificationEvent.INVALID_PAYLOAD, topic=topic_str, error="malformed topic")
            recorder.record_error(db, "malformed_topic", topic=topic_str)
        return

    device_uuid = parts[1]
    event_type = parts[2]
    # A device in Test mode publishes to the same event type with a "_test"
    # suffix (MQTT_Schema_Reference.pdf) — same shape, different handling.
    is_test = event_type.endswith("_test")
    base_event_type = event_type[: -len("_test")] if is_test else event_type

    with SessionLocal() as db:
        device = db.query(Device).filter(Device.device_uuid == device_uuid).first()
        if not device:
            logger.error(
                f"Device not found: no device registered with UUID '{device_uuid}'. "
                f"Message received on topic '{topic_str}'. "
                f"Register the device first before it can publish data."
            )
            emit(db, NotificationEvent.UNKNOWN_DEVICE, device_uuid=device_uuid, topic=topic_str)
            recorder.record_error(db, "unknown_device", topic=topic_str, device_uuid=device_uuid)
            # Track the stray UUID so the dashboard can offer one-click
            # registration instead of silently dropping its data forever.
            record_sighting(db, device_uuid, topic_str, data)
            return

        try:
            if base_event_type == "sensor_data":
                handle_sensor_data(db, device, data, is_test=is_test)

            elif base_event_type == "mosquito_data":
                handle_mosquito_event(db, device, data, is_test=is_test)

            else:
                logger.warning(f"Unknown topic pattern: '{topic_str}' — no handler matched for device '{device_uuid}'")
                return
        except Exception as e:
            # A handler bug must not kill the MQTT loop silently — surface it
            # in the System Health error feed with the session cleaned first.
            logger.exception(f"Handler failed for topic {topic_str}")
            try:
                db.rollback()
            except Exception:
                pass
            recorder.record_error(db, "handler_error", topic=topic_str,
                                  device_uuid=device_uuid, detail=str(e))
            return

        recorder.record_message(db, device.id, base_event_type, is_test)

@mqtt.on_disconnect()
def on_disconnect(client, packet, exc=None):
    logger.warning("Disconnected from MQTT broker")
    recorder.mark_disconnected()
    # Persist the disconnect into the error feed so outages have history even
    # across API restarts. Guarded: this can fire during app shutdown when the
    # DB pool is already going away.
    try:
        with SessionLocal() as db:
            recorder.record_error(db, "broker_disconnected",
                                  detail=str(exc)[:500] if exc else None)
    except Exception:
        logger.exception("Could not persist broker disconnect")


@mqtt.on_subscribe()
def on_subscribe(client, mid, qos, properties):
    logger.info(f"Subscribed to topic — mid: {mid}, qos: {qos}")
