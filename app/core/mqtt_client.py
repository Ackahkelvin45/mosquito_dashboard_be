import json
import logging
from datetime import datetime
from fastapi_mqtt import FastMQTT, MQTTConfig
from app.device.models import Device, SensorDeviceReading, MosquitoEvent, MosquitoIndividualReading
from app.core.database import SessionLocal
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BROKER = os.getenv("MQTT_BROKER", "localhost")
PORT = int(os.getenv("MQTT_PORT", 1883))
TOPIC_SENSOR_DATA = os.getenv("TOPIC_SENSOR_DATA", "mosquito_dashboard/+/sensor_data")
TOPIC_MOSQUITO_COUNT = os.getenv("TOPIC_MOSQUITO_COUNT", "mosquito_dashboard/+/mosquito_data")
CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "mosquito_dashboard_server")

mqtt_config = MQTTConfig(
    host=BROKER,
    port=PORT,
)

mqtt = FastMQTT(config=mqtt_config)


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
        "trap_status": false
    }
    """
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


@mqtt.on_connect()
def on_connect(client, flags, rc, properties):
    mqtt.client.subscribe(TOPIC_SENSOR_DATA)
    mqtt.client.subscribe(TOPIC_MOSQUITO_COUNT)
    logger.info(f"Connected to MQTT broker at {BROKER}:{PORT}")


@mqtt.on_message()
async def on_message(client, topic, payload, qos, properties):
    topic_str = topic

    try:
        data = json.loads(payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"Failed to parse message on topic {topic_str}: {e}")
        return

    # Extract device UUID from topic: mosquito_dashboard/<device_uuid>/...
    parts = topic_str.split("/")
    if len(parts) < 3:
        logger.error(f"Malformed topic received: '{topic_str}' — expected format: mosquito_dashboard/<device_uuid>/<event_type>")
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
