"""System Health / MQTT monitoring: ingest instrumentation (traffic counters,
error log, broker state), the /monitoring endpoints (SUPER_ADMIN only), the
fleet-silence watchdog and the retention cleanup."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

import app.core.mqtt_client as mqtt_client
import app.jobs.cleanup as cleanup_module
import app.jobs.pipeline_watchdog as watchdog_module
from app.authentication.enums import UserRole
from app.core.mqtt_client import on_message
from app.jobs.cleanup import run_monitoring_cleanup
from app.jobs.pipeline_watchdog import run_pipeline_watchdog
from app.monitoring import recorder
from app.monitoring.models import MqttIngestError, MqttTrafficHourly
from app.monitoring.recorder import record_error, record_message
from app.notification.enums import NotificationType
from app.notification.models import Notification


@pytest.fixture(autouse=True)
def no_geocoding(monkeypatch):
    monkeypatch.setattr(
        mqtt_client, "apply_reported_position", lambda db, device, lat, lon: None
    )


@pytest.fixture(autouse=True)
def fresh_broker_state():
    saved = dict(recorder.broker_state)
    yield
    recorder.broker_state.update(saved)


@pytest.fixture
def super_admin(make_user):
    return make_user(role=UserRole.SUPER_ADMIN)


def _sensor_payload():
    return {"timestamp": 1786270740, "temp_internal": 25.0, "temp_external": 26.0,
            "humidity_internal": 50.0, "humidity_external": 60.0,
            "pressure_internal": 1010.0, "battery": 3.9, "trap_status": "OFF"}


def _publish(topic, payload):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    asyncio.run(on_message(None, topic, payload, 0, None))


# ── Recorder ─────────────────────────────────────────────────────────────────

class TestRecorder:
    def test_record_message_upserts_same_bucket(self, db_session, make_device):
        device = make_device()
        record_message(db_session, device.id, "sensor_data", False)
        record_message(db_session, device.id, "sensor_data", False)
        rows = db_session.query(MqttTrafficHourly).all()
        assert len(rows) == 1
        assert rows[0].message_count == 2
        assert rows[0].bucket_start.minute == 0

    def test_separate_rows_per_type_and_mode(self, db_session, make_device):
        device = make_device()
        record_message(db_session, device.id, "sensor_data", False)
        record_message(db_session, device.id, "mosquito_data", False)
        record_message(db_session, device.id, "sensor_data", True)
        assert db_session.query(MqttTrafficHourly).count() == 3

    def test_record_error_truncates_and_stores(self, db_session):
        record_error(db_session, "invalid_payload", topic="t" * 300, detail="d" * 600)
        row = db_session.query(MqttIngestError).one()
        assert row.error_type == "invalid_payload"
        assert len(row.topic) == 255
        assert len(row.detail) == 500


# ── on_message instrumentation ───────────────────────────────────────────────

class TestIngestInstrumentation:
    @pytest.fixture(autouse=True)
    def patched_session_local(self, monkeypatch, TestingSessionLocal):
        monkeypatch.setattr(mqtt_client, "SessionLocal", TestingSessionLocal)

    def test_stored_message_counts_traffic_and_marks_state(self, db_session,
                                                           super_admin, make_device):
        device = make_device()
        _publish(f"mosquito_dashboard/{device.device_uuid}/sensor_data", _sensor_payload())
        row = db_session.query(MqttTrafficHourly).one()
        assert row.device_id == device.id
        assert row.event_type == "sensor_data"
        assert row.message_count == 1
        assert recorder.broker_state["last_message_at"] is not None
        assert db_session.query(MqttIngestError).count() == 0

    def test_test_topic_counts_as_test_traffic(self, db_session, super_admin,
                                               make_device):
        device = make_device()
        _publish(f"mosquito_dashboard/{device.device_uuid}/sensor_data_test",
                 _sensor_payload())
        row = db_session.query(MqttTrafficHourly).one()
        assert row.is_test is True

    def test_unknown_device_logged_as_error(self, db_session, super_admin):
        _publish("mosquito_dashboard/ghost-uuid/sensor_data", _sensor_payload())
        row = db_session.query(MqttIngestError).one()
        assert row.error_type == "unknown_device"
        assert row.device_uuid == "ghost-uuid"
        assert db_session.query(MqttTrafficHourly).count() == 0

    def test_invalid_json_logged_as_error(self, db_session, super_admin):
        _publish("mosquito_dashboard/x/sensor_data", b"{broken")
        assert db_session.query(MqttIngestError).one().error_type == "invalid_payload"

    def test_malformed_topic_logged_as_error(self, db_session, super_admin):
        _publish("nope", _sensor_payload())
        assert db_session.query(MqttIngestError).one().error_type == "malformed_topic"

    def test_handler_crash_logged_not_raised(self, db_session, super_admin,
                                             make_device, monkeypatch):
        device = make_device()

        def boom(db, dev, data, is_test=False):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(mqtt_client, "handle_sensor_data", boom)
        _publish(f"mosquito_dashboard/{device.device_uuid}/sensor_data", _sensor_payload())
        row = db_session.query(MqttIngestError).one()
        assert row.error_type == "handler_error"
        assert "kaboom" in row.detail
        assert db_session.query(MqttTrafficHourly).count() == 0  # not counted as stored


# ── Endpoints ────────────────────────────────────────────────────────────────

class TestMonitoringEndpoints:
    def test_super_admin_only(self, client, login_as, make_user):
        for role in (UserRole.USER, UserRole.ADMIN):
            login_as(make_user(role=role))
            assert client.get("/monitoring/mqtt/status").status_code == 403
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        assert client.get("/monitoring/mqtt/status").status_code == 200

    def test_status_payload(self, client, login_as, super_admin, make_device,
                            db_session):
        login_as(super_admin)
        fresh = make_device(name="Fresh")
        stale = make_device(name="Stale",
                            last_activity=datetime.utcnow() - timedelta(hours=3))
        never = make_device(name="Never", last_sensor_data_at=None)
        record_message(db_session, fresh.id, "sensor_data", False)
        record_error(db_session, "unknown_device", device_uuid="ghost")

        body = client.get("/monitoring/mqtt/status").json()
        assert body["devices_total"] == 3
        assert body["devices_reporting"] == 1
        assert body["messages_24h"] == 1
        assert body["errors_24h"] == 1
        silent_names = {d["name"] for d in body["silent_devices"]}
        assert silent_names == {"Stale", "Never"}
        assert body["offline_threshold_min"] > 0
        # In-memory state empty in tests -> falls back to fleet last_activity.
        assert body["last_message_at"] is not None

    def test_traffic_buckets_zero_filled(self, client, login_as, super_admin,
                                         make_device, db_session):
        login_as(super_admin)
        device = make_device()
        record_message(db_session, device.id, "sensor_data", False)
        record_message(db_session, device.id, "mosquito_data", False)
        record_message(db_session, device.id, "sensor_data", True)

        buckets = client.get("/monitoring/mqtt/traffic?hours=6").json()
        assert len(buckets) == 6
        latest = buckets[-1]
        assert latest["sensor_count"] == 1
        assert latest["mosquito_count"] == 1
        assert latest["test_count"] == 1
        assert all(b["sensor_count"] == 0 for b in buckets[:-1])

    def test_device_timeline_scoped_and_404(self, client, login_as, super_admin,
                                            make_device, db_session):
        login_as(super_admin)
        d1, d2 = make_device(), make_device()
        record_message(db_session, d1.id, "sensor_data", False)
        record_message(db_session, d2.id, "sensor_data", False)

        mine = client.get(f"/monitoring/mqtt/devices/{d1.id}/timeline?hours=2").json()
        assert mine[-1]["sensor_count"] == 1  # only d1's message
        assert client.get("/monitoring/mqtt/devices/999999/timeline").status_code == 404

    def test_errors_paginated_and_filterable(self, client, login_as, super_admin,
                                             db_session):
        login_as(super_admin)
        for i in range(3):
            record_error(db_session, "unknown_device", device_uuid=f"u{i}")
        record_error(db_session, "invalid_payload", topic="t")

        body = client.get("/monitoring/mqtt/errors?page=1&page_size=2").json()
        assert body["total"] == 4
        assert len(body["items"]) == 2
        assert body["total_pages"] == 2

        only = client.get("/monitoring/mqtt/errors?error_type=invalid_payload").json()
        assert only["total"] == 1
        assert only["items"][0]["topic"] == "t"


# ── Watchdog + cleanup jobs ──────────────────────────────────────────────────

class TestPipelineWatchdog:
    @pytest.fixture(autouse=True)
    def patched_sessions(self, monkeypatch, TestingSessionLocal):
        monkeypatch.setattr(watchdog_module, "SessionLocal", TestingSessionLocal)

    def _alerts(self, db_session):
        return db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.PIPELINE_SILENT
        ).all()

    def test_silent_fleet_alerts_super_admins_once(self, db_session, super_admin,
                                                   make_device):
        make_device(last_activity=datetime.utcnow() - timedelta(hours=2))
        make_device(last_activity=datetime.utcnow() - timedelta(hours=5))
        run_pipeline_watchdog()
        rows = self._alerts(db_session)
        assert len(rows) == 1
        assert rows[0].user_id == super_admin.id
        # Second sweep inside the 60-min dedupe window: no repeat.
        run_pipeline_watchdog()
        assert len(self._alerts(db_session)) == 1

    def test_active_fleet_never_alerts(self, db_session, super_admin, make_device):
        make_device(last_activity=datetime.utcnow() - timedelta(hours=2))
        make_device(last_activity=datetime.utcnow())  # one device is enough
        run_pipeline_watchdog()
        assert self._alerts(db_session) == []

    def test_empty_fleet_never_alerts(self, db_session, super_admin):
        run_pipeline_watchdog()
        assert self._alerts(db_session) == []


class TestMonitoringCleanup:
    def test_prunes_only_beyond_retention(self, db_session, make_device,
                                          monkeypatch, TestingSessionLocal):
        monkeypatch.setattr(cleanup_module, "SessionLocal", TestingSessionLocal)
        device = make_device()
        now = datetime.utcnow()
        db_session.add_all([
            MqttTrafficHourly(bucket_start=now - timedelta(days=31),
                              device_id=device.id, event_type="sensor_data",
                              is_test=False, message_count=5),
            MqttTrafficHourly(bucket_start=now, device_id=device.id,
                              event_type="sensor_data", is_test=False, message_count=1),
            MqttIngestError(occurred_at=now - timedelta(days=8),
                            error_type="invalid_payload"),
            MqttIngestError(occurred_at=now, error_type="invalid_payload"),
        ])
        db_session.commit()

        run_monitoring_cleanup()
        db_session.expire_all()
        assert db_session.query(MqttTrafficHourly).count() == 1
        assert db_session.query(MqttIngestError).count() == 1
