"""System performance list inside /model-performance — uptime and
completeness math, own-cluster scope, admin-only device measures, honest
not_measured entries, JSON keys."""
from datetime import datetime, timedelta

import pytest

from app.authentication.enums import UserRole
from app.device.models import SensorDeviceReading
from app.device.schema import OFFLINE_AFTER_MIN
from app.monitoring.system_performance import uptime
from app.trap_counts.models import ManualTrapCount
from tests.test_model_performance import _get, app, as_user, client  # noqa: F401 (fixtures)

S = datetime(2026, 1, 1)
E = S + timedelta(days=1)
H = timedelta(hours=1)


def test_uptime_math():
    # Readings all period, no long gap.
    assert uptime(S, E, S - H, S, E, 0) == 100.0
    # Gap into the period: down only after the threshold, cut to the period.
    # Reading 1 h before the period, next 1 h into it → the 1 h inside counts
    # (the threshold already passed before the period began).
    assert uptime(S, E, S - H, S + H, E, 0) == pytest.approx(100 * 23 / 24, abs=0.1)
    # Gap starting just before the period: the threshold is taken off first.
    assert uptime(S, E, S - timedelta(minutes=4), S + H, E, 0) == pytest.approx(
        100 * (1 - (60 - (OFFLINE_AFTER_MIN - 4)) / 1440), abs=0.1)
    # Short gap into the period (under the offline threshold) is not down time.
    assert uptime(S, E, S - timedelta(minutes=2), S + timedelta(minutes=2), E, 0) == 100.0
    # No reading before → measured from the first reading (23 h); the ongoing
    # gap counts minus the threshold.
    assert uptime(S, E, None, S + H, E - 2 * H, 0) == pytest.approx(
        100 * (1 - (120 - OFFLINE_AFTER_MIN) / (23 * 60)), abs=0.1)
    # Inner gaps (already reduced by the threshold in SQL) subtract too.
    assert uptime(S, E, S - H, S, E, 6 * 3600) == 75.0
    # A reading stamped a little ahead (clock ahead) is clipped to the end.
    assert uptime(S, E, S - H, S, E + timedelta(minutes=3), 0) == 100.0
    # Nothing in the period: down all period if it ever reported, else unknown.
    assert uptime(S, E, S - H, None, None, 0) == 0.0
    assert uptime(S, E, None, None, None, 0) is None


@pytest.fixture
def world(make_cluster, make_device, make_user):
    c1, c2 = make_cluster(), make_cluster(public=True)
    return {"d1": make_device(c1, name="A trap"), "quiet": make_device(c1, name="B quiet", last_sensor_data_at=None),
            "other": make_device(c2, name="Public trap", latitude=5.6, longitude=-0.2),
            "user": make_user(cluster_id=c1.id), "admin": make_user(role=UserRole.ADMIN, cluster_id=c1.id),
            "super": make_user(role=UserRole.SUPER_ADMIN)}


def _measures(body):
    return {m["key"]: m for m in body["system"]}


def test_uptime_completeness_and_scope(client, as_user, world, db_session):
    now = datetime.utcnow()
    d1 = world["d1"]
    for ts, fields in [
        (now - 23 * H, dict(external_temperature=25, internal_humidity=60, battery_pct=80)),
        (now - 23 * H + timedelta(minutes=5), dict(internal_temperature=25)),
        (now - 20 * H, {}),
    ]:
        db_session.add(SensorDeviceReading(device_id=d1.id, timestamp=ts, **fields))
    # Test-mode data never counts.
    db_session.add(SensorDeviceReading(device_id=d1.id, timestamp=now - 10 * H, is_test=True,
                                       external_temperature=1, external_humidity=1, battery_pct=1))
    db_session.add(SensorDeviceReading(device_id=world["other"].id, timestamp=now - H, external_temperature=1))
    db_session.commit()

    as_user(world["admin"])
    m = _measures(_get(client, days=1))
    up = m["uptime"]
    assert up["status"] == "measured" and up["unit"] == "%"
    # Measured from the first reading (23 h). Down: (2 h 55 min gap − threshold)
    # + (20 h ongoing − threshold).
    rows = {r["label"]: r for r in up["rows"]}
    assert set(rows) == {"A trap", "B quiet"}  # public cluster's device not shown
    down = 175 + 1200 - 2 * OFFLINE_AFTER_MIN
    assert rows["A trap"]["value"] == pytest.approx(100 * (1 - down / 1380), abs=0.15)
    assert rows["B quiet"]["value"] is None
    assert up["value"] == rows["A trap"]["value"]  # never-reported device not averaged in

    comp = m["data_completeness"]
    assert comp["name"] == "Value completeness (of readings received)"
    assert {r["label"]: r["value"] for r in comp["rows"]} == {"A trap": 44.4, "B quiet": None}
    assert comp["value"] == 44.4

    gps = m["gps_fix"]
    assert (gps["value"], gps["unit"]) == (0, "of 2 devices")

    as_user(world["super"])
    m = _measures(_get(client, days=1))
    assert {r["label"] for r in m["uptime"]["rows"]} == {"A trap", "B quiet", "Public trap"}
    assert (m["gps_fix"]["value"], m["gps_fix"]["unit"]) == (1, "of 3 devices")


def test_not_measured_entries_and_keys(client, as_user, world):
    as_user(world["user"])
    body = _get(client)
    m = _measures(body)
    for key in ["gps_accuracy", "audio_quality", "false_positives_by_zone", "false_negatives_by_zone",
                "zone_bias", "load_time", "target_detection", "target_sex", "target_age", "target_infection"]:
        assert m[key]["status"] == "not_measured" and m[key]["value"] is None and m[key]["how"], key
    assert "HDOP" in m["gps_accuracy"]["how"]
    assert "ecological-zone" in m["zone_bias"]["how"]
    assert m["target_infection"]["name"] == "Infection accuracy (target above 85%)"
    assert m["target_infection"]["period"] == "Proposal target"
    assert set(body["system"][0]) == {"key", "name", "value", "unit", "status", "period", "how", "rows"}
    assert m["accuracy_per_site"]["status"] == "no_data" and m["accuracy_per_site"]["value"] is None
    # USER: no device measures at all.
    assert not {"uptime", "data_completeness", "gps_fix"} & set(m)
    as_user(world["admin"])
    body = _get(client)
    assert {"uptime", "data_completeness", "gps_fix", "accuracy_per_site", "target_age"} <= set(_measures(body))
    assert set(body["system"][0]["rows"][0]) == {"label", "value", "detail"}


def test_accuracy_per_site(client, as_user, world, db_session, make_device):
    now = datetime.utcnow().replace(microsecond=0)
    t0, t1 = now - 48 * H, now - 24 * H
    d1 = world["d1"]
    d1.region, d1.community = "Greater Accra", "Madina"
    db_session.add(SensorDeviceReading(device_id=d1.id, timestamp=t0 + H))
    for genus, sex, n in [("aedes", "female", 0), ("culex", "male", 2)]:
        db_session.add(ManualTrapCount(device_id=d1.id, period_start=t0, period_end=t1,
                                       genus=genus, sex=sex, count=n, entered_by_user_id=1))
    # Public-cluster device sent nothing in the period → left out (not reporting).
    db_session.add(ManualTrapCount(device_id=world["other"].id, period_start=t0, period_end=t1,
                                   genus="aedes", sex="female", count=3, entered_by_user_id=1))
    db_session.commit()
    as_user(world["user"])
    site = _measures(_get(client))["accuracy_per_site"]
    assert site["status"] == "measured"
    assert "not per-detection accuracy" in site["how"]
    # aedes female: AI 0 = manual 0 (match); culex male: AI 0 ≠ 2.
    assert site["value"] == 50.0
    assert "same data as the agreement" in site["how"].lower()
    assert [(r["label"], r["value"]) for r in site["rows"]] == [("Greater Accra / Madina", 50.0)]


def test_uptime_threshold_is_exclusive():
    # A gap of exactly the offline threshold is not down time (into, or still going at the end).
    T = timedelta(minutes=OFFLINE_AFTER_MIN)
    assert uptime(S, E, S - T / 2, S + T / 2, E, 0) == 100.0
    assert uptime(S, E, S - H, S, E - T, 0) == 100.0


def test_inner_gap_threshold_pooled_completeness_and_imports(client, as_user, world, db_session, make_device,
                                                             make_user):
    from app.device.models import DataImport
    now = datetime.utcnow().replace(microsecond=0)
    c1 = world["d1"].cluster
    exact, over = make_device(c1, name="C exact"), make_device(c1, name="D over")
    T = timedelta(minutes=OFFLINE_AFTER_MIN)
    start = now - 23 * H
    # Seven readings each, the last a minute ago: gaps of exactly the threshold
    # (never down) vs threshold + 1 min (1 min down each, 6 gaps).
    for d, gap in [(exact, T), (over, T + timedelta(minutes=1))]:
        for k in range(7):
            db_session.add(SensorDeviceReading(device_id=d.id, timestamp=now - timedelta(minutes=1) - k * gap))
    # d1: one full reading (3/3) + one with an impossible battery_pct (2/3);
    # "C exact"/"D over": 0/21 each → pooled 5/48, not the average of devices.
    db_session.add(SensorDeviceReading(device_id=world["d1"].id, timestamp=start + timedelta(minutes=1),
                                       external_temperature=25, external_humidity=60, battery_pct=150))
    db_session.add(SensorDeviceReading(device_id=world["d1"].id, timestamp=start,
                                       external_temperature=25, external_humidity=60, battery_pct=80))
    # Imported rows never count, for uptime or completeness.
    batch = DataImport(kind="sensor-readings", rows_total=1, rows_imported=1, rows_skipped=0,
                       imported_by_user_id=make_user().id)
    db_session.add(batch)
    db_session.flush()
    for i in range(10):
        db_session.add(SensorDeviceReading(device_id=world["d1"].id, timestamp=start + (i + 1) * H,
                                           import_batch_id=batch.id, external_temperature=25,
                                           external_humidity=60, battery_pct=80))
    db_session.commit()

    as_user(world["admin"])
    m = _measures(_get(client, days=1))
    up = {r["label"]: r["value"] for r in m["uptime"]["rows"]}
    assert up["C exact"] == 100.0  # exact-threshold gaps are up time
    assert up["D over"] == pytest.approx(100 * (1 - 6 / 67), abs=0.2)
    # Measured from its first reading (23 h ago): only the ongoing gap minus the threshold is down.
    assert up["A trap"] == pytest.approx(100 * (OFFLINE_AFTER_MIN + 1) / 1380, abs=0.15)
    comp = m["data_completeness"]
    assert {r["label"]: r["value"] for r in comp["rows"]}["A trap"] == pytest.approx(500 / 6, abs=0.1)
    assert comp["value"] == pytest.approx(500 / 48, abs=0.1)


def test_gps_fix_counts_only_usable(client, as_user, world, db_session, make_device):
    c1 = world["d1"].cluster
    make_device(c1, name="E null island", latitude=0.0, longitude=0.0)
    make_device(c1, name="F half", latitude=5.6, longitude=None)
    make_device(c1, name="G fix", latitude=5.6, longitude=-0.2)
    as_user(world["admin"])
    gps = _measures(_get(client))["gps_fix"]
    rows = {r["label"]: r["value"] for r in gps["rows"]}
    assert (rows["E null island"], rows["F half"], rows["G fix"], rows["A trap"]) == (0.0, 0.0, 1.0, None)
    assert (gps["value"], gps["unit"]) == (1, "of 5 devices")


def test_accuracy_per_site_skips_non_reporting_and_other_clusters(client, as_user, world, db_session):
    now = datetime.utcnow().replace(microsecond=0)
    t0, t1 = now - 48 * H, now - 24 * H
    # Own device sent nothing in the period: its collection is left out.
    db_session.add(ManualTrapCount(device_id=world["d1"].id, period_start=t0, period_end=t1,
                                   genus="aedes", sex="female", count=0, entered_by_user_id=1))
    # Public-cluster device IS reporting: visible in the Agreement section, so here too.
    other = world["other"]
    other.region = "Public"
    db_session.add(SensorDeviceReading(device_id=other.id, timestamp=t0 + H))
    db_session.add(ManualTrapCount(device_id=other.id, period_start=t0, period_end=t1,
                                   genus="aedes", sex="female", count=0, entered_by_user_id=1))
    db_session.commit()
    as_user(world["user"])
    body = _get(client)
    site = _measures(body)["accuracy_per_site"]
    assert [(r["label"], r["value"]) for r in site["rows"]] == [("Public", 100.0)]
    # Same figure as the Agreement section.
    a = body["agreement"]
    assert site["value"] == round(100 * a["exact_match_share"], 1)
    assert a["collections_not_reporting"] == 1
