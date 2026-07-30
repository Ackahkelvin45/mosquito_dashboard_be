import json
import logging
from datetime import datetime
from fastapi_mqtt import FastMQTT, MQTTConfig
from app.device.models import Device, SensorDeviceReading, MosquitoEvent, MosquitoIndividualReading
from app.core.database import SessionLocal
from app.core.config import settings
from app.notification.events import NotificationEvent, emit
from app.service.device_location_service import apply_reported_position

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BROKER = settings.MQTT_BROKER
PORT = settings.MQTT_PORT
TOPIC_SENSOR_DATA = settings.TOPIC_SENSOR_DATA
TOPIC_MOSQUITO_COUNT = settings.TOPIC_MOSQUITO_COUNT
CLIENT_ID = settings.MQTT_CLIENT_ID

mqtt_config = MQTTConfig(
    host=BROKER,
    port=PORT,
    reconnect_retries=-1,
    reconnect_delay=5,
)

mqtt = FastMQTT(config=mqtt_config, client_id=CLIENT_ID)


def _parse_timestamp(value) -> datetime:
    """Parse a timestamp that may be a string or already a datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.warning(f"Could not parse timestamp '{value}', using utcnow")
    return datetime.utcnow()


def _apply_position_from_payload(db, device: Device, data: dict) -> None:
    """Update the device's position if the payload carried a GPS fix.

    Accepts a few common key spellings so firmware variations don't silently
    drop the fix, and tolerates a nested {"location": {...}} / {"gps": {...}}.
    """
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
    if lat is None or lon is None:
        return

    try:
        apply_reported_position(db, device, lat, lon)
    except Exception:
        # A bad GPS fix must never cost us the reading itself.
        logger.exception("Could not apply reported position for device %s", device.device_uuid)


def handle_sensor_data(db, device: Device, data: dict):
    """
    Handles sensor_data topic payload:
    {
        "timestamp": "...",
        "sensor_id": "ESP32_001",
        "temp_internal": 26.5,
        "temp_external": 30.2,
        "humidity_internal": 60,
        "humidity_external": 75,
        "pressure_internal": 1010,
        "external_light": 200.0,
        "battery": 3.7,
        "trap_status": false,
        "latitude": 5.6059,      # optional GPS fix — updates the device's
        "longitude": -0.1030     # position and its region/community
    }
    """
    _apply_position_from_payload(db, device, data)

    # Trap-flip detection needs the state as it was BEFORE this reading lands.
    previous = (
        db.query(SensorDeviceReading.trap_status)
        .filter(SensorDeviceReading.device_id == device.id)
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
        trap_status=data.get("trap_status", False),
    )
    db.add(reading)
    device.last_activity = datetime.utcnow()
    db.commit()
    logger.info(f"Sensor reading saved for device {device.device_uuid}")

    emit(db, NotificationEvent.LOW_BATTERY, device=device, voltage=reading.battery_voltage)
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


def handle_mosquito_event(db, device: Device, data: dict):
    """
    Handles mosquito_data topic payload:
    {
        "timestamp": "...",
        "sensor_id": "ESP32_001",
        "mosquito_data": [
            {
                "detection_timestamp": "...",
                "species": "Anopheles gambiae",
                "genus": "Anopheles",
                "age_group": "adult",
                "sex": "female"
            },
            ...
        ]
    }
    """
    mosquito_reading = data.get("mosquito_reading")
    if mosquito_reading is None:
        mosquito_data = data.get("mosquito_data")
        if isinstance(mosquito_data, dict):
            mosquito_reading = mosquito_data
        elif isinstance(mosquito_data, list):
            if len(mosquito_data) != 1:
                logger.error(
                    f"Expected exactly 1 mosquito reading but got {len(mosquito_data)} "
                    f"for device {device.device_uuid}"
                )
                return
            mosquito_reading = mosquito_data[0]

    if not isinstance(mosquito_reading, dict):
        logger.info(f"No mosquito reading in payload for device {device.device_uuid}")
        return

    _apply_position_from_payload(db, device, data)

    event = MosquitoEvent(
        device_id=device.id,
        timestamp=_parse_timestamp(data.get("timestamp")),
        count=1,
    )
    db.add(event)
    db.flush()

    individual = MosquitoIndividualReading(
        batch_id=event.id,
        detection_timestamp=_parse_timestamp(mosquito_reading.get("detection_timestamp")),
        species=mosquito_reading.get("species"),
        genus=mosquito_reading.get("genus"),
        age_group=mosquito_reading.get("age_group"),
        sex=mosquito_reading.get("sex"),
    )
    db.add(individual)

    device.total_mosquito_count = (device.total_mosquito_count or 0) + 1
    device.last_activity = datetime.utcnow()
    db.commit()
    logger.info(
        f"Mosquito event saved for device {device.device_uuid} "
        f"— 1 reading"
    )

    emit(db, NotificationEvent.SPECIES_DETECTED, device=device,
         species=mosquito_reading.get("species"), genus=mosquito_reading.get("genus"),
         sex=mosquito_reading.get("sex"), age_group=mosquito_reading.get("age_group"))
    emit(db, NotificationEvent.ACTIVITY_SURGE, device=device)


@mqtt.on_connect()
def on_connect(client, flags, rc, properties):
    logger.info(f"Connected to MQTT broker at {BROKER}:{PORT}")


@mqtt.subscribe(TOPIC_SENSOR_DATA, TOPIC_MOSQUITO_COUNT)
async def on_message(client, topic, payload, qos, properties):
    topic_str = topic

    try:
        data = json.loads(payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"Failed to parse message on topic {topic_str}: {e}")
        with SessionLocal() as db:
            emit(db, NotificationEvent.INVALID_PAYLOAD, topic=topic_str, error=str(e))
        return

    # Extract device UUID from topic: mosquito_dashboard/<device_uuid>/...
    parts = topic_str.split("/")
    if len(parts) < 3:
        logger.error(f"Malformed topic received: '{topic_str}' — expected format: mosquito_dashboard/<device_uuid>/<event_type>")
        with SessionLocal() as db:
            emit(db, NotificationEvent.INVALID_PAYLOAD, topic=topic_str, error="malformed topic")
        return

    device_uuid = parts[1]

    with SessionLocal() as db:
        device = db.query(Device).filter(Device.device_uuid == device_uuid).first()
        if not device:
            logger.error(
                f"Device not found: no device registered with UUID '{device_uuid}'. "
                f"Message received on topic '{topic_str}'. "
                f"Register the device first before it can publish data."
            )
            emit(db, NotificationEvent.UNKNOWN_DEVICE, device_uuid=device_uuid, topic=topic_str)
            return

        if "sensor_data" in topic_str:
            handle_sensor_data(db, device, data)

        elif "mosquito_data" in topic_str:
            handle_mosquito_event(db, device, data)

        else:
            logger.warning(f"Unknown topic pattern: '{topic_str}' — no handler matched for device '{device_uuid}'")

@mqtt.on_disconnect()
def on_disconnect(client, packet, exc=None):
    logger.warning("Disconnected from MQTT broker")


@mqtt.on_subscribe()
def on_subscribe(client, mid, qos, properties):
    logger.info(f"Subscribed to topic — mid: {mid}, qos: {qos}")
