"""Near-null-island rejection and region/community clearing on a real move."""
import pytest

from app.core import mqtt_client
from app.monitoring.models import MqttIngestError

import app.service.device_location_service as svc
from utils.coordinates import normalize_lat_lon

GHANA = (6.644, -1.598)


@pytest.fixture
def geocode_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(svc, "_dispatch_geocode", lambda *a: calls.append(a))
    monkeypatch.setattr(svc, "_last_geocode_attempt", {})
    return calls


@pytest.mark.parametrize("lat,lon", [(0, 0), (0.061, 0.000), (-0.5, 0.9)])
def test_no_fix_box_rejected(lat, lon):
    assert normalize_lat_lon(lat, lon) == (None, None)


@pytest.mark.parametrize("lat,lon", [(0.5, 1.5), (1.0, 0.0), (0.0, 1.0), GHANA])
def test_real_positions_accepted(lat, lon):
    assert normalize_lat_lon(lat, lon) == (lat, lon)


def test_scaled_value_still_normalises():
    lat, lon = normalize_lat_lon(6644000, -1598000)
    assert lat == pytest.approx(6.644) and lon == pytest.approx(-1.598)


def _labelled(make_device):
    return make_device(latitude=GHANA[0], longitude=GHANA[1], region="Ashanti", community="Kumasi")


def test_no_fix_report_leaves_position_and_labels(db_session, make_device, geocode_calls):
    device = _labelled(make_device)
    assert svc.apply_reported_position(db_session, device, 0.061, 0.000) is False
    assert (device.latitude, device.longitude) == GHANA
    assert (device.region, device.community) == ("Ashanti", "Kumasi")
    assert geocode_calls == []


def test_real_move_clears_labels_and_geocodes(db_session, make_device, geocode_calls):
    device = _labelled(make_device)
    assert svc.apply_reported_position(db_session, device, 6.734, -1.598) is True  # ~10 km north
    assert (device.latitude, device.longitude) == (6.734, -1.598)
    assert device.region is None and device.community is None
    assert geocode_calls == []  # not before the position is committed
    db_session.commit()
    assert geocode_calls == [(device.id, 6.734, -1.598)]


def test_jitter_keeps_labels(db_session, make_device, geocode_calls):
    device = _labelled(make_device)
    assert svc.apply_reported_position(db_session, device, 6.6441, -1.5981) is False  # ~15 m
    assert (device.latitude, device.longitude) == GHANA
    assert (device.region, device.community) == ("Ashanti", "Kumasi")
    assert geocode_calls == []


def test_first_fix_keeps_manual_labels(db_session, make_device, geocode_calls):
    device = make_device(region="Ashanti", community="Kumasi")
    assert svc.apply_reported_position(db_session, device, *GHANA) is True
    assert (device.latitude, device.longitude) == GHANA
    assert (device.region, device.community) == ("Ashanti", "Kumasi")


def test_leaving_junk_stored_position_is_first_fix(db_session, make_device, geocode_calls, monkeypatch):
    emitted = []
    monkeypatch.setattr(svc, "emit", lambda *a, **k: emitted.append(a))
    device = make_device(latitude=0.061, longitude=0.0, region="Laguna", community="Don Jose")
    assert svc.apply_reported_position(db_session, device, *GHANA) is True
    assert emitted == []  # not a "move" from 0.061,0
    assert device.region is None and device.community is None  # junk labels go too
    db_session.commit()
    assert geocode_calls == [(device.id, *GHANA)]


def test_rollback_dispatches_nothing(db_session, make_device, geocode_calls):
    device = make_device()
    svc.apply_reported_position(db_session, device, *GHANA)
    db_session.rollback()
    db_session.commit()
    assert geocode_calls == []


def _position_errors(db_session):
    return [e.detail for e in db_session.query(MqttIngestError)
            .filter(MqttIngestError.error_type == "invalid_value")
            if e.detail.startswith("latitude=")]


def test_mqtt_no_fix_is_silent_and_ignored(db_session, make_device, geocode_calls):
    # A GPS without a fix is a normal, lasting state: logging every message
    # would flood System Health.
    device = _labelled(make_device)
    mqtt_client.handle_sensor_data(db_session, device, {"latitude": 0.061, "longitude": 0.000})
    assert (device.latitude, device.longitude) == GHANA
    assert (device.region, device.community) == ("Ashanti", "Kumasi")
    assert _position_errors(db_session) == []


def test_mqtt_unreadable_fix_recorded_as_invalid_value(db_session, make_device, geocode_calls):
    device = _labelled(make_device)
    mqtt_client.handle_sensor_data(db_session, device, {"latitude": "abc", "longitude": -1.598})
    assert (device.latitude, device.longitude) == GHANA
    assert _position_errors(db_session) == ["latitude='abc' longitude=-1.598"]


def test_savepoint_release_waits_for_outer_commit(db_session, make_device, geocode_calls):
    device = make_device()
    savepoint = db_session.begin_nested()
    svc.apply_reported_position(db_session, device, *GHANA)
    savepoint.commit()  # releases the savepoint only
    assert geocode_calls == []
    db_session.commit()
    assert geocode_calls == [(device.id, *GHANA)]


def test_mqtt_null_fix_is_silent(db_session, make_device, geocode_calls):
    device = _labelled(make_device)
    mqtt_client.handle_sensor_data(db_session, device, {"latitude": None, "longitude": None})
    details = [e.detail for e in db_session.query(MqttIngestError)
               .filter(MqttIngestError.error_type == "invalid_value")]
    assert not any(d.startswith("latitude=") for d in details)


def test_savepoint_rollback_keeps_queued_geocode(db_session, make_device, geocode_calls):
    device = make_device()
    svc.apply_reported_position(db_session, device, *GHANA)
    db_session.begin_nested().rollback()  # unrelated savepoint; outer tx (and the move) survive
    db_session.commit()
    assert geocode_calls == [(device.id, *GHANA)]
