import json
import logging
from datetime import datetime
from fastapi_mqtt import FastMQTT, MQTTConfig
from app.device.models import Device, SensorDeviceReading, MosquitoEvent, MosquitoIndividualReading
from app.device.sightings import extract_raw_fix, record_sighting
from app.core.database import SessionLocal
from app.core.config import settings
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


def _parse_timestamp(value) -> datetime:
    """Parse sensor_data/mosquito_data 'timestamp' fields.

    Per MQTT_Schema_Reference.pdf the real device always sends UTC epoch
    SECONDS as an integer — that's the authoritative format. ISO strings are
    also accepted (REST callers, older test fixtures, already-parsed values).
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.utcfromtimestamp(value)
        except (ValueError, OSError, OverflowError):
            logger.warning(f"Could not parse epoch timestamp '{value}', using utcnow")
            return datetime.utcnow()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.warning(f"Could not parse timestamp '{value}', using utcnow")
    return datetime.utcnow()


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
                    SensorDeviceReading.is_test.is_(False))
            .order_by(SensorDeviceReading.timestamp.desc(), SensorDeviceReading.id.desc())
            .first()
        )
        previous_trap_on = bool(previous[0]) if previous else False

    reading = SensorDeviceReading(
        device_id=device.id,
        timestamp=_parse_timestamp(data.get("timestamp")),
        external_temperature=data.get("temp_external"),
        internal_temperature=data.get("temp_internal"),
        external_humidity=data.get("humidity_external"),
        internal_humidity=data.get("humidity_internal"),
        internal_pressure=data.get("pressure_internal"),
        external_pressure=data.get("pressure_external"),
        external_light=data.get("external_light"),
        battery_voltage=data.get("battery"),
        battery_pct=data.get("battery_pct"),
        trap_status=parse_trap_status(data.get("trap_status"), default=False),
        esp1_link_alive=data.get("esp1_link_alive"),
        is_test=is_test,
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
    if all(v is None for v in (
        reading.external_temperature, reading.internal_temperature,
        reading.external_humidity, reading.internal_humidity,
        reading.internal_pressure, reading.external_pressure,
        reading.external_light, reading.battery_voltage,
    )):
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
    outer_timestamp = _parse_timestamp(data.get("timestamp"))

    saved = 0
    for mosquito_reading in readings:
        event = MosquitoEvent(device_id=device.id, timestamp=outer_timestamp, count=1, is_test=is_test)
        db.add(event)
        db.flush()

        individual = MosquitoIndividualReading(
            batch_id=event.id,
            detection_timestamp=_parse_timestamp(mosquito_reading.get("detection_timestamp")),
            species=mosquito_reading.get("species"),
            genus=mosquito_reading.get("genus"),
            # age_group/sex columns are NOT NULL; the schema guarantees the
            # keys (age_group is always "" today), but a malformed entry must
            # degrade to "" rather than blow up the whole message's commit.
            age_group=mosquito_reading.get("age_group") or "",
            sex=mosquito_reading.get("sex") or "",
            p_mosq=mosquito_reading.get("p_mosq"),
            binary_decision=mosquito_reading.get("binary_decision"),
            taxon_probs=mosquito_reading.get("taxon_probs"),
            sex_probs=mosquito_reading.get("sex_probs"),
            inference_ms=mosquito_reading.get("inference_ms"),
        )
        db.add(individual)
        saved += 1

        if not is_test:
            emit(db, NotificationEvent.SPECIES_DETECTED, device=device,
                 species=mosquito_reading.get("species"), genus=mosquito_reading.get("genus"),
                 sex=mosquito_reading.get("sex"), age_group=mosquito_reading.get("age_group"))

    device.last_activity = datetime.utcnow()
    if is_test:
        db.commit()
        logger.info(f"{saved} TEST mosquito event(s) saved for device {device.device_uuid} — not counted")
        return

    device.total_mosquito_count = (device.total_mosquito_count or 0) + saved
    db.commit()
    logger.info(f"{saved} mosquito event(s) saved for device {device.device_uuid}")

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
