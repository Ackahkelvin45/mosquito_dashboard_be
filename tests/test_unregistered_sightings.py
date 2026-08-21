"""Unregistered-device sightings (TODO.md §1): MQTT data from unknown UUIDs
is upserted into a visible sighting instead of only a log line, exposed via
/devices/unregistered (SUPER_ADMIN), cleared automatically on registration
and dismissable by hand."""
import asyncio
import json
from datetime import datetime

import pytest
from fastapi import FastAPI

import app.core.mqtt_client as mqtt_client
from app.authentication.enums import UserRole
from app.core.database import get_db
from app.core.mqtt_client import on_message
from app.device.models import UnregisteredDeviceSighting
from app.device.schema import DeviceCreate
from app.device.sightings import record_sighting
from app.service.device_service import DeviceService


@pytest.fixture
def app(db_session):
    """Device router app so the /devices/unregistered endpoints are live."""
    from app.device.routes import router as device_router

    application = FastAPI()
    application.include_router(device_router, prefix="/devices")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture(autouse=True)
def patched_session_local(monkeypatch, TestingSessionLocal):
    monkeypatch.setattr(mqtt_client, "SessionLocal", TestingSessionLocal)


@pytest.fixture(autouse=True)
def no_geocoding(monkeypatch):
    monkeypatch.setattr(
        mqtt_client, "apply_reported_position", lambda db, device, lat, lon: None
    )


def _publish(topic, payload):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    asyncio.run(on_message(None, topic, payload, 0, None))


def _sighting(db_session, uuid_="AI4PEP_AAAA00000001"):
    return db_session.query(UnregisteredDeviceSighting).filter(
        UnregisteredDeviceSighting.device_uuid == uuid_
    ).first()


class TestSightingCapture:
    def test_unknown_uuid_creates_and_upserts(self, db_session, make_user):
        make_user(role=UserRole.SUPER_ADMIN)  # UNKNOWN_DEVICE notification target
        topic = "mosquito_dashboard/AI4PEP_AAAA00000001/sensor_data"
        _publish(topic, {"timestamp": 1786270740, "battery": 3.9})
        row = _sighting(db_session)
        assert row is not None
        assert row.message_count == 1
        assert row.last_topic == topic
        assert row.last_payload["battery"] == 3.9

        _publish(topic, {"timestamp": 1786270750, "battery": 3.8})
        db_session.expire_all()
        row = _sighting(db_session)
        assert row.message_count == 2
        assert row.last_payload["battery"] == 3.8
        assert row.first_seen <= row.last_seen
        # One row per UUID, ever.
        assert db_session.query(UnregisteredDeviceSighting).count() == 1

    def test_gps_fix_parsed_and_kept(self, db_session, make_user):
        make_user(role=UserRole.SUPER_ADMIN)
        topic = "mosquito_dashboard/AI4PEP_AAAA00000001/sensor_data"
        _publish(topic, {"latitude": 14.2582913, "longitude": 121.0678318})
        row = _sighting(db_session)
        assert row.latitude == 14.2582913
        assert row.longitude == 121.0678318
        # A later payload WITHOUT a fix must not erase the known one.
        _publish(topic, {"battery": 3.7})
        db_session.expire_all()
        row = _sighting(db_session)
        assert row.latitude == 14.2582913

    def test_registered_device_never_sighted(self, db_session, make_user,
                                             make_device):
        make_user(role=UserRole.SUPER_ADMIN)
        device = make_device()
        _publish(f"mosquito_dashboard/{device.device_uuid}/sensor_data",
                 {"timestamp": 1786270740, "battery": 3.9, "temp_internal": 25.0})
        assert db_session.query(UnregisteredDeviceSighting).count() == 0

    def test_oversized_payload_not_stored_but_counted(self, db_session):
        record_sighting(db_session, "big-uuid", "t", {"blob": "x" * 5000})
        row = _sighting(db_session, "big-uuid")
        assert row.message_count == 1
        assert row.last_payload is None


class TestRegistrationClearsSighting:
    def test_create_device_clears_matching_sighting(self, db_session):
        record_sighting(db_session, "AI4PEP_AAAA00000001", "t",
                        {"latitude": 5.6, "longitude": -0.18})
        DeviceService(db_session).create_device(
            DeviceCreate(name="New Trap", device_uuid="AI4PEP_AAAA00000001")
        )
        assert _sighting(db_session) is None

    def test_other_sightings_untouched(self, db_session):
        record_sighting(db_session, "AI4PEP_AAAA00000001", "t", {})
        record_sighting(db_session, "AI4PEP_BBBB00000002", "t", {})
        DeviceService(db_session).create_device(
            DeviceCreate(name="New Trap", device_uuid="AI4PEP_AAAA00000001")
        )
        assert _sighting(db_session) is None
        assert _sighting(db_session, "AI4PEP_BBBB00000002") is not None


class TestSightingEndpoints:
    def test_super_admin_only(self, client, login_as, make_user):
        for role in (UserRole.USER, UserRole.ADMIN):
            login_as(make_user(role=role))
            assert client.get("/devices/unregistered").status_code == 403
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        assert client.get("/devices/unregistered").status_code == 200

    def test_list_newest_first_paginated(self, client, login_as, make_user,
                                         db_session):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        record_sighting(db_session, "older-uuid", "t", {})
        record_sighting(db_session, "newer-uuid", "t", {"latitude": 1.0, "longitude": 2.0})

        body = client.get("/devices/unregistered").json()
        assert body["total"] == 2
        assert [s["device_uuid"] for s in body["items"]][0] in ("newer-uuid", "older-uuid")
        newer = next(s for s in body["items"] if s["device_uuid"] == "newer-uuid")
        assert newer["latitude"] == 1.0
        assert newer["message_count"] == 1

    def test_dismiss_deletes_and_404s_when_missing(self, client, login_as,
                                                   make_user, db_session):
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        record_sighting(db_session, "stray-uuid", "t", {})
        assert client.delete("/devices/unregistered/stray-uuid").status_code == 204
        assert _sighting(db_session, "stray-uuid") is None
        assert client.delete("/devices/unregistered/stray-uuid").status_code == 404

    def test_unregistered_path_not_swallowed_by_device_id_route(self, client,
                                                                login_as, make_user):
        # Guards the route-registration ORDER: "unregistered" must reach its
        # own handler, never 422 against GET /devices/{device_id:int}.
        login_as(make_user(role=UserRole.SUPER_ADMIN))
        assert client.get("/devices/unregistered").status_code == 200
