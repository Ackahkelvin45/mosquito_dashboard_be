"""Test-mode rows (is_test=True, from "_test" MQTT topics) must never reach a
read path: dashboard, device charts, Sensor Data / Historical lists, filter
options, public API, summaries, latest_reading, surge count, stuck-sensor
check. Each test seeds one live and one test row and asserts only the live
one is seen. The test row is always the NEWER one, so a "latest" lookup that
ignores is_test would pick it."""
from datetime import datetime, timedelta

import pytest

import app.jobs.health as health_module
import app.jobs.summaries as summaries_module
from app.api_access.queries import mosquito_events_stmt, sensor_readings_stmt
from app.authentication.enums import UserRole
from app.device.models import MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading
from app.device.repository.device_repository import DeviceRepository
from app.notification import events
from app.notification.enums import NotificationType
from app.notification.events import NotificationEvent, emit
from app.notification.models import Notification
from app.service.dashboard_service import DashboardService
from app.service.device_chart_service import DeviceChartService


def _event(db, device, ts, *, is_test, count, genus, species, sex):
    event = MosquitoEvent(device_id=device.id, timestamp=ts, count=count, is_test=is_test)
    db.add(event)
    db.flush()
    db.add(MosquitoIndividualReading(
        batch_id=event.id, detection_timestamp=ts, genus=genus,
        species=species, sex=sex, age_group="",
    ))
    return event


@pytest.fixture
def world(db_session, make_device, make_user):
    make_user(role=UserRole.SUPER_ADMIN)  # notification recipient
    device = make_device(region="R1", community="C1")
    now = datetime.utcnow()
    live_reading = SensorDeviceReading(
        device_id=device.id, timestamp=now - timedelta(hours=1),
        external_temperature=25.0, external_humidity=70.0,
        battery_voltage=3.5, trap_status=False, is_test=False,
    )
    test_reading = SensorDeviceReading(
        device_id=device.id, timestamp=now - timedelta(minutes=30),
        external_temperature=40.0, external_humidity=20.0,
        battery_voltage=1.0, trap_status=True, is_test=True,
    )
    db_session.add_all([live_reading, test_reading])
    live_event = _event(db_session, device, now - timedelta(hours=1), is_test=False,
                        count=1, genus="aedes", species="Aedes sp.", sex="female")
    test_event = _event(db_session, device, now - timedelta(minutes=30), is_test=True,
                        count=10, genus="culex", species="Culex sp.", sex="male")
    db_session.commit()
    return {"device": device, "now": now,
            "live_reading": live_reading, "test_reading": test_reading,
            "live_event": live_event, "test_event": test_event}


class TestDashboard:
    def test_totals_and_charts_exclude_test_rows(self, db_session, world):
        gb = {k: "day" for k in (
            "totals_group_by", "chart_group_by", "gender_group_by", "region_group_by",
            "sensor_status_group_by", "breakdown_group_by", "correlation_group_by",
            "genus_heatmap_group_by", "hourly_group_by")}
        res = DashboardService(db_session).get_dashboard(**gb)

        assert res.totals.total_mosquito_count == 1
        assert res.totals.average_battery_voltage == 3.5
        assert res.chart.total == 1
        assert (res.gender_distribution.male, res.gender_distribution.female) == (0, 1)
        assert [r.count for r in res.region_chart.data] == [1]
        assert {i.name.lower() for i in res.breakdown.genus} == {"aedes"}
        assert sum(p.mosquito_count for p in res.correlation_chart.data) == 1
        assert {p.temperature for p in res.correlation_chart.data if p.temperature is not None} == {25.0}
        assert [g.lower() for g in res.genus_heatmap.genera] == ["aedes"]
        # Latest LIVE reading has the trap OFF; the newer test reading says ON.
        last = res.sensor_status_chart.data[-1]
        assert (last.on_count, last.off_count) == (0, 1)


class TestDeviceCharts:
    def test_device_charts_exclude_test_rows(self, db_session, world):
        charts = DeviceChartService(db_session).get_device_charts(world["device"].id, "day")
        assert sum(p.count for p in charts.mosquito_count.data) == 1
        assert sum(p.male for p in charts.mosquito_gender.data) == 0
        assert {p.external for p in charts.temperature.data if p.external is not None} == {25.0}
        assert {p.voltage for p in charts.battery.data if p.voltage is not None} == {3.5}
        assert charts.sensor_status.data[-1].on_count == 0


class TestLists:
    def test_sensor_data_page(self, db_session, world):
        rows, total = DeviceRepository(db_session).get_sensor_readings_page(1, 50)
        assert [r.id for r in rows] == [world["live_reading"].id]
        assert total == 1

    def test_per_device_sensor_readings(self, db_session, world):
        rows = DeviceRepository(db_session).get_sensor_readings(world["device"].id)
        assert [r.id for r in rows] == [world["live_reading"].id]

    def test_historical_list_plain_and_joined(self, db_session, world):
        repo = DeviceRepository(db_session)
        live_id = world["live_event"].id
        assert [e.id for e in repo.get_all_mosquito_events()] == [live_id]
        assert [e.id for e in repo.get_all_mosquito_events(region=["R1"])] == [live_id]
        assert [e.id for e in repo.get_all_mosquito_events(search="sp")] == [live_id]

    def test_per_device_mosquito_events(self, db_session, world):
        repo = DeviceRepository(db_session)
        device_id = world["device"].id
        assert [e.id for e in repo.get_mosquito_events(device_id)] == [world["live_event"].id]
        assert [e.id for e in repo.get_mosquito_events(device_id, search="sp")] == [world["live_event"].id]

    def test_filter_options(self, db_session, world):
        opts = DeviceRepository(db_session).get_mosquito_filter_options()
        assert opts["genus"] == ["aedes"]
        assert opts["species"] == ["Aedes sp."]

    def test_trap_status_device_filter_uses_latest_live_reading(self, db_session, world):
        repo = DeviceRepository(db_session)
        assert repo.filter_devices(trap_status=True) == []
        assert [d.id for d in repo.filter_devices(trap_status=False)] == [world["device"].id]


class TestPublicApi:
    def test_sensor_readings_stmt(self, db_session, world):
        ids = db_session.execute(sensor_readings_stmt(None, ["id"])).scalars().all()
        assert ids == [world["live_reading"].id]

    def test_mosquito_events_stmt(self, db_session, world):
        ids = db_session.execute(mosquito_events_stmt(None, ["id"])).scalars().all()
        assert ids == [world["live_event"].id]


class TestLatestReadingAndSummaries:
    def test_latest_reading_ignores_newer_test_reading(self, db_session, world):
        device = world["device"]
        db_session.expire(device)
        assert device.latest_reading.id == world["live_reading"].id

    def test_summary_stats(self, db_session, world):
        stats = summaries_module._device_group_stats(
            db_session, [world["device"]], world["now"] - timedelta(days=1))
        assert stats["mosquito_events"] == 1
        assert stats["mosquito_total"] == 1
        assert stats["top_species"] == "Aedes sp."
        # The test reading's 1.0 V would count as critical if it leaked in.
        assert stats["low_battery_devices"] == 0


class TestSurgeCount:
    def test_test_events_do_not_count_toward_surge(self, db_session, make_device, make_user):
        make_user(role=UserRole.SUPER_ADMIN)
        device = make_device()
        now = datetime.utcnow()
        threshold = events.NOTIFY_SURGE_THRESHOLD
        for is_test in (False, True):
            for _ in range(threshold):
                db_session.add(MosquitoEvent(device_id=device.id, timestamp=now,
                                             count=1, is_test=is_test))
        db_session.commit()

        def surges():
            return db_session.query(Notification).filter(
                Notification.notification_type == NotificationType.ACTIVITY_SURGE).all()

        emit(db_session, NotificationEvent.ACTIVITY_SURGE, device=device)
        assert surges() == []  # threshold live (+ threshold test) is not a surge

        db_session.add(MosquitoEvent(device_id=device.id, timestamp=now, count=1))
        db_session.commit()
        emit(db_session, NotificationEvent.ACTIVITY_SURGE, device=device)
        rows = surges()
        assert len(rows) == 1
        assert rows[0].payload["count"] == threshold + 1


class TestStuckSensor:
    def test_newer_test_reading_does_not_mask_stuck_live_sensor(
            self, db_session, make_device, make_user, monkeypatch, TestingSessionLocal):
        monkeypatch.setattr(health_module, "SessionLocal", TestingSessionLocal)
        make_user(role=UserRole.SUPER_ADMIN)
        device = make_device(last_activity=datetime.utcnow())
        now = datetime.utcnow()
        for index in range(1, 6):
            db_session.add(SensorDeviceReading(
                device_id=device.id, timestamp=now - timedelta(minutes=index),
                external_temperature=25.0, internal_temperature=26.0,
                external_humidity=70.0, internal_humidity=60.0, battery_voltage=3.8,
            ))
        db_session.add(SensorDeviceReading(
            device_id=device.id, timestamp=now, external_temperature=31.0,
            battery_voltage=3.8, is_test=True,
        ))
        db_session.commit()
        health_module.run_health_sweep()
        rows = db_session.query(Notification).filter(
            Notification.notification_type == NotificationType.SENSOR_MALFUNCTION).all()
        assert len(rows) == 1
