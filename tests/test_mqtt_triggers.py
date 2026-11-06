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
        # sensor_data is the liveness heartbeat.
        assert (datetime.utcnow() - device.last_sensor_data_at).total_seconds() < 5
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

    def test_imported_trap_on_row_does_not_suppress_flip(self, db_session, super_admin, make_device):
        # A CSV-imported reading (import_batch_id set) is history, not the
        # device's current state: a later-timestamped imported trap=ON must
        # not hide the real OFF->ON flip.
        from app.device.models import DataImport
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(trap_status=False))
        batch = DataImport(kind="sensor-readings", rows_total=1, rows_imported=1, rows_skipped=0,
                           imported_by_user_id=super_admin.id)
        db_session.add(batch)
        db_session.flush()
        db_session.add(SensorDeviceReading(device_id=device.id, timestamp=datetime(2026, 7, 30, 11),
                                           trap_status=True, import_batch_id=batch.id))
        db_session.commit()
        handle_sensor_data(db_session, device, _sensor_payload(trap_status=True))
        assert _types(db_session).count(NotificationType.TRAP_TRIGGERED) == 1

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

    def test_epoch_timestamp_parsed(self, db_session, super_admin, make_device):
        # Real device firmware always sends UTC epoch seconds as an int —
        # the authoritative format per MQTT_Schema_Reference.pdf.
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(timestamp=1786270740))
        reading = db_session.query(SensorDeviceReading).one()
        assert reading.timestamp == datetime(2026, 8, 9, 10, 19, 0)

    def test_trap_status_on_off_strings(self, db_session, super_admin, make_device):
        # Real device firmware sends "ON"/"OFF" strings, not booleans.
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(trap_status="ON"))
        reading = db_session.query(SensorDeviceReading).one()
        assert reading.trap_status is True
        handle_sensor_data(db_session, device, _sensor_payload(trap_status="OFF"))
        readings = db_session.query(SensorDeviceReading).order_by(SensorDeviceReading.id).all()
        assert readings[-1].trap_status is False

    def test_battery_pct_and_esp1_link_alive_persisted(self, db_session, super_admin,
                                                        make_device):
        device = make_device()
        handle_sensor_data(
            db_session, device,
            _sensor_payload(battery_pct=100, esp1_link_alive=False),
        )
        reading = db_session.query(SensorDeviceReading).one()
        assert reading.battery_pct == 100
        assert reading.esp1_link_alive is False

    def test_low_battery_uses_pct_when_present(self, db_session, super_admin, make_device):
        # Voltage ranges are battery-profile-dependent (a 3-cell pack reads
        # ~9-12.6V) — a flat voltage threshold would never fire for it, so
        # pct (already normalised) must take priority when present.
        device = make_device()
        handle_sensor_data(
            db_session, device,
            _sensor_payload(battery=9.5, battery_pct=15),
        )
        rows = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.LOW_BATTERY
        ).all()
        assert len(rows) == 1
        assert rows[0].payload["battery_pct"] == 15

    def test_test_mode_reading_persisted_without_notifications(self, db_session,
                                                                super_admin, make_device):
        # A device in Test mode (the "_test"-suffixed MQTT topic) still needs
        # its data stored so a technician can confirm connectivity, but a
        # payload that would normally alert (dead battery + trap on) must
        # never page anyone.
        device = make_device()
        handle_sensor_data(
            db_session, device,
            _sensor_payload(battery=2.5, battery_pct=1, trap_status=True),
            is_test=True,
        )
        reading = db_session.query(SensorDeviceReading).one()
        assert reading.is_test is True
        assert reading.trap_status is True
        assert (datetime.utcnow() - device.last_activity).total_seconds() < 5
        assert _types(db_session) == []

    def test_test_mode_does_not_perturb_live_trap_flip_state(self, db_session,
                                                              super_admin, make_device):
        # Cycling the trap while testing must not consume or corrupt the
        # live trap-flip detection for real readings that follow.
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(trap_status=False))
        handle_sensor_data(
            db_session, device,
            _sensor_payload(trap_status=True), is_test=True,
        )
        handle_sensor_data(db_session, device, _sensor_payload(trap_status=True))
        rows = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.TRAP_TRIGGERED
        ).all()
        assert len(rows) == 1  # only the real false->true flip, not the test one


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

    def test_multiple_readings_all_processed(self, db_session, super_admin, make_device):
        # The schema is explicit: mosquito_data is an array, don't assume
        # length 1 always holds — every entry must be saved, none dropped.
        device = make_device()
        payload = _mosquito_payload()
        payload["mosquito_data"] = [payload["mosquito_data"][0]] * 3
        handle_mosquito_event(db_session, device, payload)
        assert db_session.query(MosquitoEvent).count() == 3
        assert db_session.query(MosquitoIndividualReading).count() == 3
        assert device.total_mosquito_count == 3

    def test_mosquito_confidence_fields_persisted(self, db_session, super_admin, make_device):
        device = make_device()
        payload = _mosquito_payload(
            p_mosq=0.87, binary_decision=True, inference_ms=215,
            taxon_probs={"anopheles": 0, "aedes": 0.91, "culex": 0},
            sex_probs={"male": 0, "female": 0.85},
        )
        handle_mosquito_event(db_session, device, payload)
        reading = db_session.query(MosquitoIndividualReading).one()
        assert reading.p_mosq == 0.87
        assert reading.binary_decision is True
        assert reading.inference_ms == 215
        assert reading.taxon_probs == {"anopheles": 0, "aedes": 0.91, "culex": 0}
        assert reading.sex_probs == {"male": 0, "female": 0.85}

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

    def test_mosquito_event_does_not_stamp_liveness_heartbeat(self, db_session,
                                                              super_admin, make_device):
        # mosquito_data refreshes last_activity ("last seen") but NOT
        # last_sensor_data_at — a detection burst must not mask a dead
        # telemetry loop in the eyes of the offline detector.
        stale = datetime.utcnow() - timedelta(hours=2)
        device = make_device(last_sensor_data_at=stale)
        handle_mosquito_event(db_session, device, _mosquito_payload())
        assert (datetime.utcnow() - device.last_activity).total_seconds() < 5
        assert device.last_sensor_data_at == stale

    def test_test_mode_event_not_counted_or_alerted(self, db_session, super_admin,
                                                     make_device):
        # A vector-species detection while a device is in Test mode must be
        # stored (for verification) but never inflate the real count or
        # page anyone — it isn't a real surveillance event.
        device = make_device()
        handle_mosquito_event(db_session, device, _mosquito_payload(), is_test=True)
        event = db_session.query(MosquitoEvent).one()
        assert event.is_test is True
        assert device.total_mosquito_count == 0
        assert (datetime.utcnow() - device.last_activity).total_seconds() < 5
        assert _types(db_session) == []

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

    def test_sensor_test_topic_routed_as_test_data(self, db_session, super_admin,
                                                    make_device):
        # A device in Test mode publishes to "<...>/sensor_data_test" — the
        # MQTT "+" wildcard used for the live topic subscription does NOT
        # match this (it only matches a whole segment), so this topic needs
        # its own subscription; a low-battery payload here must be stored
        # but never alert.
        device = make_device()
        self._publish(
            f"mosquito_dashboard/{device.device_uuid}/sensor_data_test",
            _sensor_payload(battery=3.0, battery_pct=5),
        )
        reading = db_session.query(SensorDeviceReading).one()
        assert reading.is_test is True
        assert _types(db_session) == []

    def test_mosquito_test_topic_routed_as_test_data(self, db_session, super_admin,
                                                      make_device):
        device = make_device()
        self._publish(
            f"mosquito_dashboard/{device.device_uuid}/mosquito_data_test",
            _mosquito_payload(),
        )
        event = db_session.query(MosquitoEvent).one()
        assert event.is_test is True
        assert device.total_mosquito_count == 0
        assert _types(db_session) == []

    def test_live_and_test_topics_produce_independent_rows(self, db_session,
                                                            super_admin, make_device):
        device = make_device()
        self._publish(
            f"mosquito_dashboard/{device.device_uuid}/sensor_data_test",
            _sensor_payload(),
        )
        self._publish(
            f"mosquito_dashboard/{device.device_uuid}/sensor_data",
            _sensor_payload(),
        )
        readings = db_session.query(SensorDeviceReading).order_by(SensorDeviceReading.id).all()
        assert [r.is_test for r in readings] == [True, False]


class TestUnregisteredDeviceRecovery:
    """The UNKNOWN_DEVICE alert tells an admin to register the device — this
    covers that the recovery it prescribes actually works end to end."""

    @pytest.fixture(autouse=True)
    def patched_session_local(self, monkeypatch, TestingSessionLocal):
        monkeypatch.setattr(mqtt_client, "SessionLocal", TestingSessionLocal)

    def _publish(self, uuid_, payload=None):
        body = json.dumps(payload or _sensor_payload()).encode()
        asyncio.run(on_message(
            None, f"mosquito_dashboard/{uuid_}/sensor_data", body, 0, None))

    def test_repeated_unknown_messages_alert_once_then_data_flows_after_register(
        self, db_session, super_admin
    ):
        from app.device.schema import DeviceCreate
        from app.service.device_service import DeviceService

        uuid_ = "AI4PEP_083AF28F1930"

        # Firmware publishes every few seconds — one alert, not one per message.
        for _ in range(5):
            self._publish(uuid_)
        alerts = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.UNKNOWN_DEVICE
        ).all()
        assert len(alerts) == 1
        assert alerts[0].user_id == super_admin.id
        assert db_session.query(SensorDeviceReading).count() == 0

        # The alert says "register the device" — so registering by UUID alone,
        # with no cluster, has to work.
        DeviceService(db_session).create_device(
            DeviceCreate(name="Trap 083A", device_uuid=uuid_)
        )

        self._publish(uuid_)
        assert db_session.query(SensorDeviceReading).count() == 1
        # ...and the alert stops.
        assert db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.UNKNOWN_DEVICE
        ).count() == 1

    def test_unknown_device_realerts_once_the_window_lapses(
        self, db_session, super_admin, backdate_dedupe_key
    ):
        uuid_ = "AI4PEP_FFFFFFFFFFFF"

        def alert_count():
            return db_session.query(Notification).filter(
                Notification.notification_type == NotificationType.UNKNOWN_DEVICE
            ).count()

        self._publish(uuid_)
        assert alert_count() == 1
        self._publish(uuid_)
        assert alert_count() == 1  # still inside the dedupe window
        # A device left unregistered should keep nagging, not go quiet forever.
        backdate_dedupe_key(f"unknown_device:{uuid_}", minutes=61)
        self._publish(uuid_)
        assert alert_count() == 2


def test_test_topic_names_are_derived_from_the_live_topic_names():
    # Guards the "_test" suffix convention itself — MQTT_Schema_Reference.pdf
    # is explicit that Test-mode devices publish to <live topic>_test.
    assert mqtt_client.TOPIC_SENSOR_DATA_TEST == f"{mqtt_client.TOPIC_SENSOR_DATA}_test"
    assert mqtt_client.TOPIC_MOSQUITO_COUNT_TEST == f"{mqtt_client.TOPIC_MOSQUITO_COUNT}_test"


class TestSchemaReferenceConformance:
    """Field-by-field fidelity against MQTT_Schema_Reference.pdf, using the
    AUTHORITATIVE wire format the real firmware sends: epoch-second integer
    timestamps, trap_status as "ON"/"OFF" strings, and null for "no valid
    reading" (which must reach the DB as NULL, never 0)."""

    # The PDF's own example values.
    SENSOR_WIRE = {
        "timestamp": 1786270740,
        "local_time": "2026-08-09 18:19:00",   # convenience only — ignored
        "sensor_id": "AI4PEP_6825DD44B768",
        "device_label": "Philippine_Trap_2",   # informational — ignored
        "latitude": 14.2582913,
        "longitude": 121.0678318,
        "temp_internal": 29.4,
        "humidity_internal": 45.8,
        "pressure_internal": 1012.3,
        "temp_external": 27.8,
        "humidity_external": 78.5,
        "pressure_external": 1010.2,
        "rainfall": None,                      # hardware not deployed — ignored
        "battery": 12.64043,
        "battery_pct": 100,
        "trap_status": "ON",
        "esp1_link_alive": False,
    }

    MOSQUITO_WIRE = {
        "timestamp": 1786270740,
        "local_time": "2026-08-09 18:19:00",
        "sensor_id": "AI4PEP_6825DD44B768",
        "device_label": "Philippine_Trap_2",
        "mosquito_data": [
            {
                "detection_timestamp": 1786270680,
                "species": "Aedes sp.",
                "genus": "aedes",
                "age_group": "",
                "sex": "female",
                "p_mosq": 0.87,
                "binary_decision": True,
                "taxon_probs": {"anopheles": 0, "aedes": 0.91, "culex": 0},
                "sex_probs": {"male": 0, "female": 0.85},
                "species_probs": {"Anopheles gambiae": 0, "Aedes aegypti": 0,
                                  "Culex quinquefasciatus": 0, "Aedes sp.": 0},
                "inference_ms": 215,
            }
        ],
    }

    def test_sensor_data_wire_format_stored_faithfully(self, db_session, super_admin,
                                                       make_device, no_geocoding):
        device = make_device()
        handle_sensor_data(db_session, device, dict(self.SENSOR_WIRE))

        r = db_session.query(SensorDeviceReading).one()
        # Epoch seconds is the authoritative time.
        assert r.timestamp == datetime.utcfromtimestamp(1786270740)
        assert r.internal_temperature == 29.4
        assert r.internal_humidity == 45.8
        assert r.internal_pressure == 1012.3
        assert r.external_temperature == 27.8
        assert r.external_humidity == 78.5
        assert r.external_pressure == 1010.2
        assert r.battery_voltage == 12.64043
        assert r.battery_pct == 100
        assert r.trap_status is True          # "ON" string, not a boolean
        assert r.esp1_link_alive is False
        assert r.is_test is False
        # GPS fix was forwarded to the position pipeline.
        assert no_geocoding == [(device.id, 14.2582913, 121.0678318)]
        # Liveness heartbeat stamped by sensor_data.
        assert (datetime.utcnow() - device.last_sensor_data_at).total_seconds() < 5

    def test_null_means_null_never_zero(self, db_session, super_admin, make_device):
        """PDF: null = 'no valid reading' — must never be coerced to 0."""
        wire = dict(self.SENSOR_WIRE)
        wire.update({"temp_internal": None, "humidity_internal": None,
                     "pressure_internal": None, "esp1_link_alive": None,
                     "battery_pct": None, "latitude": None, "longitude": None})
        handle_sensor_data(db_session, make_device(), wire)
        r = db_session.query(SensorDeviceReading).one()
        assert r.internal_temperature is None
        assert r.internal_humidity is None
        assert r.internal_pressure is None
        assert r.esp1_link_alive is None
        assert r.battery_pct is None

    def test_trap_status_off_string(self, db_session, super_admin, make_device):
        handle_sensor_data(db_session, make_device(),
                           dict(self.SENSOR_WIRE, trap_status="OFF"))
        assert db_session.query(SensorDeviceReading).one().trap_status is False

    def test_mosquito_data_wire_format_stored_faithfully(self, db_session, super_admin,
                                                         make_device):
        device = make_device()
        before_total = device.total_mosquito_count or 0
        handle_mosquito_event(db_session, device, json.loads(json.dumps(self.MOSQUITO_WIRE)))

        event = db_session.query(MosquitoEvent).one()
        assert event.timestamp == datetime.utcfromtimestamp(1786270740)  # event END
        assert event.count == 1
        assert event.is_test is False

        i = db_session.query(MosquitoIndividualReading).one()
        assert i.batch_id == event.id
        assert i.detection_timestamp == datetime.utcfromtimestamp(1786270680)  # START
        assert i.detection_timestamp <= event.timestamp
        assert i.species == "Aedes sp."
        assert i.genus == "aedes"
        assert i.age_group == ""
        assert i.sex == "female"
        assert i.p_mosq == 0.87
        assert i.binary_decision is True
        # Confidence objects stored verbatim (winning class + zeros — NOT softmax).
        assert i.taxon_probs == {"anopheles": 0, "aedes": 0.91, "culex": 0}
        assert i.sex_probs == {"male": 0, "female": 0.85}
        assert i.inference_ms == 215
        assert device.total_mosquito_count == before_total + 1

    def test_multi_entry_array_every_entry_saved(self, db_session, super_admin,
                                                 make_device):
        """PDF: 'treat as an array, don't assume length 1 will always hold'."""
        device = make_device()
        wire = json.loads(json.dumps(self.MOSQUITO_WIRE))
        second = dict(wire["mosquito_data"][0], genus="culex", sex="male")
        wire["mosquito_data"].append(second)
        handle_mosquito_event(db_session, device, wire)
        assert db_session.query(MosquitoEvent).count() == 2
        genera = {i.genus for i in db_session.query(MosquitoIndividualReading).all()}
        assert genera == {"aedes", "culex"}
        assert device.total_mosquito_count == 2

    def test_missing_sex_and_age_group_degrade_to_empty(self, db_session, super_admin,
                                                        make_device):
        """sex/age_group columns are NOT NULL; a malformed entry missing them
        must still be stored (as "") instead of aborting the whole commit."""
        wire = json.loads(json.dumps(self.MOSQUITO_WIRE))
        del wire["mosquito_data"][0]["sex"]
        del wire["mosquito_data"][0]["age_group"]
        handle_mosquito_event(db_session, make_device(), wire)
        i = db_session.query(MosquitoIndividualReading).one()
        assert i.sex == ""
        assert i.age_group == ""

    def test_end_to_end_via_on_message_pdf_topic(self, db_session, super_admin,
                                                 monkeypatch, TestingSessionLocal):
        """Full dispatch with the PDF's real topic shape and sensor_id-style UUID."""
        monkeypatch.setattr(mqtt_client, "SessionLocal", TestingSessionLocal)
        from app.device.models import Device as DeviceModel
        device = DeviceModel(device_uuid="AI4PEP_6825DD44B768", name="Philippine_Trap_2")
        db_session.add(device)
        db_session.commit()

        asyncio.run(on_message(
            None, "mosquito_dashboard/AI4PEP_6825DD44B768/sensor_data",
            json.dumps(self.SENSOR_WIRE).encode(), 0, None,
        ))
        r = db_session.query(SensorDeviceReading).one()
        assert r.device_id == device.id
        assert r.timestamp == datetime.utcfromtimestamp(1786270740)


class TestReceivedAt:
    """Every live ingest path stamps the server receive time (naive UTC);
    the device timestamp is kept as sent. Test-mode rows are stamped too."""

    def _fresh(self, value):
        assert value is not None and abs((datetime.utcnow() - value).total_seconds()) < 5

    @pytest.mark.parametrize("is_test", [False, True])
    def test_mqtt_sensor(self, db_session, super_admin, make_device, is_test):
        handle_sensor_data(db_session, make_device(), _sensor_payload(), is_test=is_test)
        r = db_session.query(SensorDeviceReading).one()
        self._fresh(r.received_at)
        assert r.timestamp == datetime(2026, 7, 30, 10, 0)

    @pytest.mark.parametrize("is_test", [False, True])
    def test_mqtt_mosquito(self, db_session, super_admin, make_device, is_test):
        handle_mosquito_event(db_session, make_device(), _mosquito_payload(), is_test=is_test)
        e = db_session.query(MosquitoEvent).one()
        self._fresh(e.received_at)
        assert e.timestamp == datetime(2026, 7, 30, 10, 0)

    def test_http_paths(self, db_session, make_device, monkeypatch):
        import app.device.repository.device_repository as repo_module
        from app.device.schema import MosquitoEventPayload, SensorDataPayload

        monkeypatch.setattr(repo_module, "apply_reported_position", lambda *a: None)
        device, ts = make_device(), datetime(2026, 7, 30, 10, 0)
        repo = repo_module.DeviceRepository(db_session)
        repo.create_sensor_reading(device, SensorDataPayload(
            timestamp=ts, temp_external=30.0, temp_internal=28.0, humidity_external=70.0,
            humidity_internal=50.0, pressure_internal=1010.0, pressure_external=1009.0, battery=3.9))
        repo.create_mosquito_event(device, MosquitoEventPayload(
            timestamp=ts, mosquito_reading={"detection_timestamp": ts, "age_group": "", "sex": "female"}))
        # Person-authenticated manual/scripted posts, possibly of old data:
        # not received live from a device, so no receive time (like CSV import).
        assert db_session.query(SensorDeviceReading).one().received_at is None
        assert db_session.query(MosquitoEvent).one().received_at is None

    @pytest.mark.parametrize("ts", [None, "not-a-date"])
    def test_defaulted_timestamp_is_not_measured(self, db_session, super_admin, make_device, ts):
        # The timestamp falls back to server time, so a delay would be a fake ~0.
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(timestamp=ts))
        mosquito = _mosquito_payload()
        mosquito["timestamp"] = ts
        handle_mosquito_event(db_session, device, mosquito)
        r, e = db_session.query(SensorDeviceReading).one(), db_session.query(MosquitoEvent).one()
        assert r.received_at is None and e.received_at is None
        self._fresh(r.timestamp)   # existing fallback unchanged
        self._fresh(e.timestamp)

    def test_tz_aware_timestamp_stored_as_naive_utc(self, db_session, super_admin, make_device):
        handle_sensor_data(db_session, make_device(), _sensor_payload(timestamp="2026-07-29T16:00:00+03:00"))
        r = db_session.query(SensorDeviceReading).one()
        assert r.timestamp == datetime(2026, 7, 29, 13, 0) and r.timestamp.tzinfo is None


class TestImpossibleValues:
    """battery_pct / p_mosq are range-checked by the CSV import; ingest applies
    the same rule (app.device.imports.parse_bounded) so every export re-imports.
    An impossible value is stored as NULL — never clamped — the rest of the
    reading is kept, and the raw value is logged as an "invalid_value" error."""

    @staticmethod
    def _errors(db_session):
        from app.monitoring.models import MqttIngestError
        return [(e.error_type, e.device_uuid, e.detail) for e in db_session.query(MqttIngestError)]

    @pytest.mark.parametrize("is_test", [False, True])
    @pytest.mark.parametrize("bad, detail", [
        (255, "battery_pct=255"), (-1, "battery_pct=-1"), (87.5, "battery_pct=87.5"),
        ("abc", "battery_pct='abc'"), (float("nan"), "battery_pct=nan"), (True, "battery_pct=True"),
    ])
    def test_invalid_battery_pct_stored_null(self, db_session, super_admin, make_device, bad, detail, is_test):
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(battery_pct=bad, esp1_link_alive=True),
                           is_test=is_test)
        reading = db_session.query(SensorDeviceReading).one()
        assert reading.battery_pct is None
        assert (reading.external_temperature, reading.battery_voltage, reading.esp1_link_alive,
                reading.is_test) == (30.2, 3.7, True, is_test)
        assert self._errors(db_session) == [("invalid_value", device.device_uuid, detail)]

    @pytest.mark.parametrize("good, stored", [(0, 0), (100, 100), (87, 87), (87.0, 87), ("87.0", 87), (" 42 ", 42)])
    def test_valid_battery_pct_untouched(self, db_session, super_admin, make_device, good, stored):
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(battery_pct=good))
        reading = db_session.query(SensorDeviceReading).one()
        assert reading.battery_pct == stored and type(reading.battery_pct) is int
        assert self._errors(db_session) == []

    def test_missing_battery_pct_is_not_an_error(self, db_session, super_admin, make_device):
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(battery_pct=None))
        assert db_session.query(SensorDeviceReading).one().battery_pct is None
        assert self._errors(db_session) == []

    @pytest.mark.parametrize("is_test", [False, True])
    @pytest.mark.parametrize("bad, detail", [
        (1.3, "p_mosq=1.3"), (-0.1, "p_mosq=-0.1"), (float("nan"), "p_mosq=nan"),
        (float("inf"), "p_mosq=inf"), ("abc", "p_mosq='abc'"),
    ])
    def test_invalid_p_mosq_stored_null(self, db_session, super_admin, make_device, bad, detail, is_test):
        device = make_device()
        handle_mosquito_event(db_session, device, _mosquito_payload(p_mosq=bad, inference_ms=215),
                              is_test=is_test)
        individual = db_session.query(MosquitoIndividualReading).one()
        assert individual.p_mosq is None
        assert (individual.genus, individual.sex, individual.inference_ms) == ("Anopheles", "female", 215)
        assert db_session.query(MosquitoEvent).one().is_test is is_test
        db_session.refresh(device)
        assert device.total_mosquito_count == (0 if is_test else 1)
        assert self._errors(db_session) == [("invalid_value", device.device_uuid, detail)]

    @pytest.mark.parametrize("good", [0, 1, 0.0, 1.0, 0.87, "0.5"])
    def test_valid_p_mosq_untouched(self, db_session, super_admin, make_device, good):
        device = make_device()
        handle_mosquito_event(db_session, device, _mosquito_payload(p_mosq=good))
        assert db_session.query(MosquitoIndividualReading).one().p_mosq == float(good)
        assert self._errors(db_session) == []

    def test_one_error_row_per_bad_detection(self, db_session, super_admin, make_device):
        device = make_device()
        payload = _mosquito_payload(p_mosq=2)
        payload["mosquito_data"].append(dict(payload["mosquito_data"][0], p_mosq=0.4))
        payload["mosquito_data"].append(dict(payload["mosquito_data"][0], p_mosq=-3))
        handle_mosquito_event(db_session, device, payload)
        assert sorted(r.p_mosq is None for r in db_session.query(MosquitoIndividualReading)) == [False, True, True]
        assert [e[2] for e in self._errors(db_session)] == ["p_mosq=2", "p_mosq=-3"]

    def test_detail_truncated_to_500(self, db_session, super_admin, make_device):
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(battery_pct="x" * 2000))
        assert db_session.query(SensorDeviceReading).one().battery_pct is None
        assert len(self._errors(db_session)[0][2]) == 500

    # ── Every other field: numbers / flags / text / JSON ─────────────────────

    @pytest.mark.parametrize("key, bad, column", [
        ("temp_external", "abc", "external_temperature"),        # Postgres would refuse the row
        ("humidity_internal", float("nan"), "internal_humidity"),
        ("pressure_external", float("inf"), "external_pressure"),
        ("pressure_internal", "1e999", "internal_pressure"),
        ("humidity_external", "n/a", "external_humidity"),
        ("battery", [3.7], "battery_voltage"),
        ("external_light", True, "external_light"),
        ("esp1_link_alive", "yes", "esp1_link_alive"),           # Boolean column raises today
        ("esp1_link_alive", 2, "esp1_link_alive"),
    ])
    def test_invalid_sensor_field_stored_null(self, db_session, super_admin, make_device, key, bad, column):
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(**{key: bad}, battery_pct=80))
        reading = db_session.query(SensorDeviceReading).one()
        assert getattr(reading, column) is None
        assert reading.battery_pct == 80 and reading.internal_temperature == 26.5
        assert self._errors(db_session) == [("invalid_value", device.device_uuid, f"{key}={bad!r}")]

    @pytest.mark.parametrize("key, good, column, stored", [
        ("temp_external", "27.5", "external_temperature", 27.5),
        ("temp_external", -40, "external_temperature", -40.0),
        ("esp1_link_alive", 1, "esp1_link_alive", True),
        ("esp1_link_alive", False, "esp1_link_alive", False),
    ])
    def test_valid_sensor_field_untouched(self, db_session, super_admin, make_device, key, good, column, stored):
        device = make_device()
        handle_sensor_data(db_session, device, _sensor_payload(**{key: good}))
        assert getattr(db_session.query(SensorDeviceReading).one(), column) == stored
        assert self._errors(db_session) == []

    @pytest.mark.parametrize("key, bad, column, blank", [
        ("species", "x" * 251, "species", None),                 # over String(250): blank, not truncated
        ("genus", "a\x00b", "genus", None),                      # NUL: import rejects, Postgres refuses
        ("species", {"name": "aedes"}, "species", None),         # not text
        ("sex", "f" * 51, "sex", ""),                            # NOT NULL: blank is "" as for a missing sex
        ("age_group", 7, "age_group", ""),
        ("binary_decision", "maybe", "binary_decision", None),
        ("inference_ms", "fast", "inference_ms", None),
        ("inference_ms", 12.5, "inference_ms", None),
        ("taxon_probs", {"aedes": float("nan")}, "taxon_probs", None),
        ("species", "Aedes \ud800", "species", None),            # lone surrogate: not encodable UTF-8
        ("sex_probs", {"\udfff": 0.5}, "sex_probs", None),
    ])
    def test_invalid_detection_field_blank(self, db_session, super_admin, make_device, key, bad, column, blank):
        device = make_device()
        handle_mosquito_event(db_session, device, _mosquito_payload(**{key: bad}, p_mosq=0.9))
        individual = db_session.query(MosquitoIndividualReading).one()
        assert getattr(individual, column) == blank
        assert individual.p_mosq == 0.9 and individual.detection_timestamp is not None
        assert self._errors(db_session) == [("invalid_value", device.device_uuid, f"{key}={bad!r}"[:500])]

    def test_valid_detection_fields_untouched(self, db_session, super_admin, make_device):
        device = make_device()
        handle_mosquito_event(db_session, device, _mosquito_payload(
            species="s" * 250, sex="f" * 50, binary_decision=True, inference_ms=215,
            taxon_probs={"aedes": 0.91, "culex": 0}))
        i = db_session.query(MosquitoIndividualReading).one()
        assert (len(i.species), len(i.sex), i.binary_decision, i.inference_ms, i.taxon_probs) == (
            250, 50, True, 215, {"aedes": 0.91, "culex": 0})
        assert self._errors(db_session) == []

    # ── Defaults that are stored but logged (NOT NULL columns) ───────────────

    @pytest.mark.parametrize("raw, stored", [("garbage", False), (2, True), ([], False), (None, False)])
    def test_unrecognised_trap_status_logged(self, db_session, super_admin, make_device, raw, stored):
        device = make_device()
        payload = _sensor_payload(trap_status=raw)
        if raw is None:
            del payload["trap_status"]
        handle_sensor_data(db_session, device, payload)
        assert db_session.query(SensorDeviceReading).one().trap_status is stored
        assert self._errors(db_session) == [("invalid_value", device.device_uuid, f"trap_status={raw!r}")]

    @pytest.mark.parametrize("raw", ["ON", " off ", True, False, 1, 0])
    def test_recognised_trap_status_not_logged(self, db_session, super_admin, make_device, raw):
        handle_sensor_data(db_session, make_device(), _sensor_payload(trap_status=raw))
        assert self._errors(db_session) == []

    @pytest.mark.parametrize("raw", ["not-a-date", None, True])
    def test_defaulted_sensor_timestamp_logged(self, db_session, super_admin, make_device, raw):
        device = make_device()
        payload = _sensor_payload(timestamp=raw)
        if raw is None:
            del payload["timestamp"]
        handle_sensor_data(db_session, device, payload)
        reading = db_session.query(SensorDeviceReading).one()
        assert reading.received_at is None
        assert (datetime.utcnow() - reading.timestamp).total_seconds() < 5
        assert self._errors(db_session) == [("invalid_value", device.device_uuid, f"timestamp={raw!r}")]

    def test_defaulted_mosquito_timestamps_logged(self, db_session, super_admin, make_device):
        device = make_device()
        payload = _mosquito_payload(detection_timestamp="yesterday")
        payload["timestamp"] = "soon"
        handle_mosquito_event(db_session, device, payload)
        assert db_session.query(MosquitoEvent).one().received_at is None
        assert [e[2] for e in self._errors(db_session)] == ["timestamp='soon'", "detection_timestamp='yesterday'"]


class TestNotificationNeverCostsADetection:
    """SPECIES_DETECTED runs after the commit, with the stored (checked)
    values: a failing emit rolls the session back, which must not discard the
    event/reading or leave total_mosquito_count counting rows that were lost."""

    def test_emits_after_commit_with_stored_values(self, db_session, super_admin, make_device, monkeypatch):
        device = make_device()
        calls = []

        def fake_emit(db, event, **ctx):
            # Everything is already committed when any notification runs.
            assert not db.new and not db.dirty
            calls.append((str(event), ctx.get("species"), ctx.get("sex")))
            db.rollback()  # what emit does when a handler poisons the session

        monkeypatch.setattr(mqtt_client, "emit", fake_emit)
        handle_mosquito_event(db_session, device, _mosquito_payload(species="x" * 251, sex="f" * 51))
        assert calls[0] == (str(mqtt_client.NotificationEvent.SPECIES_DETECTED), None, "")
        assert db_session.query(MosquitoIndividualReading).count() == 1
        assert db_session.query(MosquitoEvent).count() == 1
        db_session.refresh(device)
        assert device.total_mosquito_count == 1

    def test_failing_notification_keeps_every_detection(self, db_session, super_admin, make_device, monkeypatch):
        from app.notification import events
        device = make_device()

        def boom(service, **_):
            # As a failed notification flush on Postgres: the session is rolled back.
            service.session.rollback()
            raise RuntimeError("notification insert failed")
        monkeypatch.setitem(events._HANDLERS, events.NotificationEvent.SPECIES_DETECTED, boom)
        payload = _mosquito_payload()
        payload["mosquito_data"].append(dict(payload["mosquito_data"][0]))
        handle_mosquito_event(db_session, device, payload)
        assert db_session.query(MosquitoIndividualReading).count() == 2
        db_session.refresh(device)
        assert device.total_mosquito_count == 2

    def test_long_species_dedupe_key_fits_and_stays_distinct(self, db_session, super_admin, make_device):
        device = make_device()
        for tail in ("a", "b", "a"):
            handle_mosquito_event(db_session, device, _mosquito_payload(
                species="Aedes " + "x" * 243 + tail, genus="aedes"))
        rows = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.SPECIES_DETECTED).all()
        keys = {n.dedupe_key for n in rows}
        # Two different 250-char species: two notifications; the repeat is deduped.
        assert len(rows) == 2 and len(keys) == 2
        assert all(len(k) <= 255 for k in keys)
