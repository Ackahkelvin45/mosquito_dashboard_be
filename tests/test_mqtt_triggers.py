"""MQTT ingestion triggers — handle_sensor_data / handle_mosquito_event called
directly with fake payloads, plus the on_message dispatch (unknown device /
invalid payload / topic routing) with SessionLocal patched to the test DB."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

import app.core.mqtt_client as mqtt_client
from app.authentication.enums import UserRole
from app.core.mqtt_client import handle_mosquito_event, handle_sensor_data, on_message
from app.device.models import MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading
from app.notification.enums import NotificationSeverity, NotificationType
from app.notification.models import Notification


@pytest.fixture
def super_admin(make_user):
    # Devices in these tests carry no cluster -> notify_cluster(None) targets
    # super admins only.
    return make_user(role=UserRole.SUPER_ADMIN)


@pytest.fixture(autouse=True)
def no_geocoding(monkeypatch):
    """GPS handling calls apply_reported_position (worker thread + Nominatim);
    replace it with a recorder so ingestion tests stay offline."""
    calls = []
    monkeypatch.setattr(
        mqtt_client, "apply_reported_position",
        lambda db, device, lat, lon: calls.append((device.id, lat, lon)),
    )
    return calls


def _types(db_session):
    return [n.notification_type for n in db_session.query(Notification).all()]


def _sensor_payload(**overrides):
    payload = {
        "timestamp": "2026-07-30T10:00:00",
        "sensor_id": "ESP32_001",
        "temp_internal": 26.5,
        "temp_external": 30.2,
        "humidity_internal": 60,
        "humidity_external": 75,
        "pressure_internal": 1010,
        "external_light": 200.0,
        "battery": 3.7,
        "trap_status": False,
    }
    payload.update(overrides)
    return payload


def _mosquito_payload(**reading_overrides):
    reading = {
        "detection_timestamp": "2026-07-30T10:00:00",
        "species": "Anopheles gambiae",
        "genus": "Anopheles",
        "age_group": "adult",
        "sex": "female",
    }
    reading.update(reading_overrides)
    return {
        "timestamp": "2026-07-30T10:00:00",
        "sensor_id": "ESP32_001",
        "mosquito_data": [reading],
    }


class TestHandleSensorData:
    def test_reading_persisted_and_activity_bumped(self, db_session, super_admin,
                                                   make_device):
        device = make_device(last_activity=datetime.utcnow() - timedelta(hours=2))
        handle_sensor_data(db_session, device, _sensor_payload())
        reading = db_session.query(SensorDeviceReading).one()
        assert reading.device_id == device.id
        assert reading.external_temperature == 30.2
        assert reading.battery_voltage == 3.7
        assert reading.timestamp == datetime(2026, 7, 30, 10, 0, 0)
        assert (datetime.utcnow() - device.last_activity).total_seconds() < 5
        # Healthy payload: no notifications at all.
        assert _types(db_session) == []

    def test_low_battery_triggers_notification(self, db_session, super_admin,
                                               make_device):
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(battery=3.1))
        rows = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.LOW_BATTERY
        ).all()
        assert len(rows) == 1
        assert rows[0].user_id == super_admin.id
        assert rows[0].severity == NotificationSeverity.WARNING

    def test_trap_transition_false_to_true_only(self, db_session, super_admin,
                                                make_device, backdate_dedupe_key):
        device = make_device()

        def trap_rows():
            return db_session.query(Notification).filter(
                Notification.notification_type == NotificationType.TRAP_TRIGGERED
            ).count()

        handle_sensor_data(db_session, device, _sensor_payload(trap_status=False))
        assert trap_rows() == 0
        handle_sensor_data(db_session, device, _sensor_payload(trap_status=True))
        assert trap_rows() == 1  # the flip
        handle_sensor_data(db_session, device, _sensor_payload(trap_status=True))
        assert trap_rows() == 1  # still on -> no re-alert
        # Even past the dedupe window: no flip means no event at all.
        backdate_dedupe_key(f"trap:{device.id}", minutes=61)
        handle_sensor_data(db_session, device, _sensor_payload(trap_status=True))
        assert trap_rows() == 1
        # Off then on again -> a new alert (window already re-armed above).
        handle_sensor_data(db_session, device, _sensor_payload(trap_status=False))
        handle_sensor_data(db_session, device, _sensor_payload(trap_status=True))
        assert trap_rows() == 2

    def test_first_reading_with_trap_on_triggers(self, db_session, super_admin,
                                                 make_device):
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(trap_status=True))
        assert NotificationType.TRAP_TRIGGERED in _types(db_session)

    def test_extreme_temperature_external(self, db_session, super_admin, make_device):
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(temp_external=50.0))
        assert NotificationType.EXTREME_TEMPERATURE in _types(db_session)

    def test_extreme_temperature_falls_back_to_internal(self, db_session, super_admin,
                                                        make_device):
        device = make_device()
        handle_sensor_data(
            db_session, device,
            _sensor_payload(temp_external=None, temp_internal=50.0),
        )
        assert NotificationType.EXTREME_TEMPERATURE in _types(db_session)

    def test_extreme_humidity(self, db_session, super_admin, make_device):
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(humidity_external=99))
        assert NotificationType.EXTREME_HUMIDITY in _types(db_session)

    def test_all_empty_sensor_fields_is_malfunction(self, db_session, super_admin,
                                                    make_device):
        device = make_device()
        empty = {key: None for key in (
            "temp_internal", "temp_external", "humidity_internal",
            "humidity_external", "pressure_internal", "pressure_external",
            "external_light", "battery",
        )}
        handle_sensor_data(db_session, device, _sensor_payload(**empty))
        rows = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.SENSOR_MALFUNCTION
        ).all()
        assert len(rows) == 1
        assert "empty" in rows[0].body

    def test_gps_fix_forwarded_to_position_service(self, db_session, super_admin,
                                                   make_device, no_geocoding):
        device = make_device()
        handle_sensor_data(
            db_session, device,
            _sensor_payload(latitude=5.6059, longitude=-0.1030),
        )
        assert no_geocoding == [(device.id, 5.6059, -0.1030)]

    def test_bad_timestamp_falls_back_to_utcnow(self, db_session, super_admin,
                                                make_device):
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(timestamp="not-a-date"))
        reading = db_session.query(SensorDeviceReading).one()
        assert (datetime.utcnow() - reading.timestamp).total_seconds() < 5


class TestHandleMosquitoEvent:
    def test_event_persisted_and_species_alert(self, db_session, super_admin,
                                               make_device):
        device = make_device()
        handle_mosquito_event(db_session, device, _mosquito_payload())
        event = db_session.query(MosquitoEvent).one()
        reading = db_session.query(MosquitoIndividualReading).one()
        assert reading.batch_id == event.id
        assert reading.species == "Anopheles gambiae"
        assert device.total_mosquito_count == 1
        rows = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.SPECIES_DETECTED
        ).all()
        assert len(rows) == 1
        assert rows[0].severity == NotificationSeverity.CRITICAL  # adult female
        assert rows[0].user_id == super_admin.id

    def test_non_vector_species_no_alert(self, db_session, super_admin, make_device):
        device = make_device()
        handle_mosquito_event(
            db_session, device,
            _mosquito_payload(species="Toxorhynchites splendens",
                              genus="Toxorhynchites"),
        )
        assert db_session.query(MosquitoEvent).count() == 1  # stored anyway
        assert NotificationType.SPECIES_DETECTED not in _types(db_session)

    def test_multiple_readings_rejected(self, db_session, super_admin, make_device):
        device = make_device()
        payload = _mosquito_payload()
        payload["mosquito_data"] = [payload["mosquito_data"][0]] * 2
        handle_mosquito_event(db_session, device, payload)
        assert db_session.query(MosquitoEvent).count() == 0
        assert _types(db_session) == []

    def test_missing_reading_ignored(self, db_session, super_admin, make_device):
        device = make_device()
        handle_mosquito_event(db_session, device, {"timestamp": "2026-07-30T10:00:00"})
        assert db_session.query(MosquitoEvent).count() == 0

    def test_dict_mosquito_data_accepted(self, db_session, super_admin, make_device):
        device = make_device()
        payload = _mosquito_payload()
        payload["mosquito_data"] = payload["mosquito_data"][0]  # dict, not list
        handle_mosquito_event(db_session, device, payload)
        assert db_session.query(MosquitoEvent).count() == 1

    def test_surge_emitted_when_threshold_crossed(self, db_session, super_admin,
                                                  make_device):
        from app.notification.events import NOTIFY_SURGE_THRESHOLD

        device = make_device()
        now = datetime.utcnow()
        for _ in range(NOTIFY_SURGE_THRESHOLD):  # 20 pre-existing + 1 new = 21
            db_session.add(MosquitoEvent(device_id=device.id, timestamp=now, count=1))
        db_session.commit()
        # The new event must land inside the rolling surge window, so it needs
        # a current timestamp — the payload default is a fixed past date.
        payload = _mosquito_payload()
        payload["timestamp"] = now.isoformat()
        handle_mosquito_event(db_session, device, payload)
        assert NotificationType.ACTIVITY_SURGE in _types(db_session)


class TestOnMessage:
    """on_message drives topic parsing + device lookup; SessionLocal is patched
    onto the test engine so the full dispatch path runs against sqlite."""

    @pytest.fixture(autouse=True)
    def patched_session_local(self, monkeypatch, TestingSessionLocal):
        monkeypatch.setattr(mqtt_client, "SessionLocal", TestingSessionLocal)

    def _publish(self, topic, payload):
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode()
        asyncio.run(on_message(None, topic, payload, 0, None))

    def test_unknown_device_notifies_super_admins(self, db_session, super_admin,
                                                  make_user):
        make_user()  # regular user should not be notified
        self._publish("mosquito_dashboard/ghost-uuid/sensor_data", _sensor_payload())
        rows = db_session.query(Notification).all()
        assert [r.notification_type for r in rows] == [NotificationType.UNKNOWN_DEVICE]
        assert rows[0].user_id == super_admin.id
        assert rows[0].payload["device_uuid"] == "ghost-uuid"

    def test_invalid_json_payload(self, db_session, super_admin):
        self._publish("mosquito_dashboard/some-uuid/sensor_data", b"{not json")
        rows = db_session.query(Notification).all()
        assert [r.notification_type for r in rows] == [NotificationType.INVALID_PAYLOAD]

    def test_malformed_topic(self, db_session, super_admin):
        self._publish("too_short", _sensor_payload())
        rows = db_session.query(Notification).all()
        assert [r.notification_type for r in rows] == [NotificationType.INVALID_PAYLOAD]

    def test_known_device_sensor_topic_routed(self, db_session, super_admin,
                                              make_device):
        device = make_device()
        self._publish(
            f"mosquito_dashboard/{device.device_uuid}/sensor_data",
            _sensor_payload(battery=3.0),
        )
        assert db_session.query(SensorDeviceReading).count() == 1
        assert NotificationType.LOW_BATTERY in _types(db_session)

    def test_known_device_mosquito_topic_routed(self, db_session, super_admin,
                                                make_device):
        device = make_device()
        self._publish(
            f"mosquito_dashboard/{device.device_uuid}/mosquito_data",
            _mosquito_payload(),
        )
        assert db_session.query(MosquitoEvent).count() == 1

    def test_unknown_event_type_ignored(self, db_session, super_admin, make_device):
        device = make_device()
        self._publish(
            f"mosquito_dashboard/{device.device_uuid}/other_topic",
            _sensor_payload(),
        )
        assert db_session.query(SensorDeviceReading).count() == 0
        assert db_session.query(Notification).count() == 0
