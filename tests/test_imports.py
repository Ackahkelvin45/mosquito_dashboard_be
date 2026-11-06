"""/imports — CSV batch import in the /api/v1/export format: round trip,
dry run, all-or-nothing, scope, duplicates, NULLs, no side effects, batch
delete, audit, limits."""
import csv
import io
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api_access.auth as api_auth
import app.device.imports as imports
from app.api_access.models import ApiKey
from app.audit.models import AuditAction, AuditLog
from app.authentication.enums import UserRole
from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.device.models import (
    DataImport, Device, MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading,
)
from app.notification.models import Notification
from utils.protected_route import get_current_user, get_current_user_or_guest

T0 = datetime(2026, 3, 1, 12, 0, 0, 123456)
# Helpers write the pre-battery_pct/p_mosq/exclusion header, i.e. an older export file.
OLDER_MISSING = imports.OPTIONAL_COLUMNS | {"excluded", "exclusion_reason"}  # = | EXCLUSION_COLUMNS
SENSOR_HEADER = ",".join(c for c in imports.EXPORT_COLUMNS[imports.SENSOR] if c not in OLDER_MISSING)
MOSQUITO_HEADER = ",".join(c for c in imports.EXPORT_COLUMNS[imports.MOSQUITO] if c not in OLDER_MISSING)
# With battery_pct / p_mosq, but from before the exclusion columns.
EXCLUSION_COLUMNS = {"excluded", "exclusion_reason"}
FULL_SENSOR_HEADER = ",".join(c for c in imports.EXPORT_COLUMNS[imports.SENSOR] if c not in EXCLUSION_COLUMNS)
FULL_MOSQUITO_HEADER = ",".join(c for c in imports.EXPORT_COLUMNS[imports.MOSQUITO] if c not in EXCLUSION_COLUMNS)


@pytest.fixture
def app(db_session):
    from app.api_access.public_routes import router as public_router

    application = FastAPI()
    application.include_router(imports.router, prefix="/imports")
    application.include_router(public_router, prefix="/api/v1")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def export_plumbing(monkeypatch, TestingSessionLocal):
    import app.api_access.public_routes as public_routes
    monkeypatch.setattr(public_routes, "SessionLocal", TestingSessionLocal)
    api_auth._windows.clear()
    yield
    api_auth._windows.clear()


@pytest.fixture
def as_user(app):
    def _as(user):
        response_user = UserResponse.model_validate(user)
        app.dependency_overrides[get_current_user] = lambda: response_user
        app.dependency_overrides[get_current_user_or_guest] = lambda: response_user
    return _as


@pytest.fixture
def world(make_cluster, make_device, make_user):
    c1, c2 = make_cluster(), make_cluster()
    return {
        "c1": c1, "c2": c2,
        "d1": make_device(c1), "d1b": make_device(c1), "d2": make_device(c2),
        "user": make_user(cluster_id=c1.id),
        "admin": make_user(role=UserRole.ADMIN, cluster_id=c1.id),
        "admin2": make_user(role=UserRole.ADMIN, cluster_id=c2.id),
        "super": make_user(role=UserRole.SUPER_ADMIN),
    }


def post(client, kind, text, dry_run=False):
    return client.post(f"/imports/{kind}", params={"dry_run": dry_run, "filename": "f.csv"},
                       content=text.encode(), headers={"Content-Type": "text/csv"})


def sensor_csv(*rows):
    """rows: (device, timestamp_cell, ext_temp_cell, trap_cell)"""
    lines = [SENSOR_HEADER]
    for device, ts, temp, trap in rows:
        lines.append(f",{ts},{device.device_uuid},,,,,{temp},,,,,,,,{trap}")
    return "\n".join(lines) + "\n"


def mosquito_csv(*rows):
    """rows: (device, timestamp_cell, species_cell)"""
    lines = [MOSQUITO_HEADER]
    for device, ts, species in rows:
        lines.append(f",{ts},{device.device_uuid},,,,,1,{ts},{species},aedes,,female")
    return "\n".join(lines) + "\n"


def export(client, db, owner, dataset):
    raw, prefix, key_hash = ApiKey.generate()
    db.add(ApiKey(name="k", key_prefix=prefix, key_hash=key_hash, user_id=owner.id))
    db.commit()
    res = client.get(f"/api/v1/export?dataset={dataset}&format=csv", headers={"X-API-Key": raw})
    assert res.status_code == 200
    return res.text


def without_id(text):
    return [row[1:] for row in csv.reader(io.StringIO(text))]


def wipe_rows(db):
    db.query(MosquitoIndividualReading).delete()
    db.query(MosquitoEvent).delete()
    db.query(SensorDeviceReading).delete()
    db.query(Device).update({"total_mosquito_count": 0})
    db.commit()


# ── Round trip ───────────────────────────────────────────────────────────────

def test_round_trip_export_import_is_identical(client, as_user, world, db_session):
    d1, d2 = world["d1"], world["d2"]
    db_session.add_all([
        SensorDeviceReading(device_id=d1.id, timestamp=T0, external_temperature=-3.25,
                            internal_humidity=55.123456789, battery_voltage=12.6, battery_pct=87,
                            trap_status=True),
        SensorDeviceReading(device_id=d2.id, timestamp=T0 + timedelta(minutes=5), trap_status=False),
    ])
    e1 = MosquitoEvent(device_id=d1.id, timestamp=T0, count=1)
    e2 = MosquitoEvent(device_id=d2.id, timestamp=T0, count=1)  # no detection row
    db_session.add_all([e1, e2])
    db_session.flush()
    db_session.add(MosquitoIndividualReading(batch_id=e1.id, detection_timestamp=T0, species="=SUM(A1)",
                                             genus="aedes", age_group="", sex="female", p_mosq=0.873))
    d1.total_mosquito_count = 1
    db_session.commit()

    sensors = export(client, db_session, world["super"], "sensor-readings")
    events = export(client, db_session, world["super"], "mosquito-events")
    wipe_rows(db_session)

    as_user(world["super"])
    assert post(client, "sensor-readings", sensors).status_code == 200
    assert post(client, "mosquito-events", events).status_code == 200

    assert without_id(export(client, db_session, world["super"], "sensor-readings")) == without_id(sensors)
    assert without_id(export(client, db_session, world["super"], "mosquito-events")) == without_id(events)
    db_session.refresh(d1)
    db_session.refresh(d2)
    assert (d1.total_mosquito_count, d2.total_mosquito_count) == (1, 0)
    assert db_session.query(MosquitoIndividualReading).one().species == "=SUM(A1)"
    assert db_session.query(MosquitoIndividualReading).one().p_mosq == 0.873
    assert sorted(r.battery_pct is None for r in db_session.query(SensorDeviceReading)) == [False, True]
    assert db_session.query(SensorDeviceReading).filter_by(device_id=d1.id).one().battery_pct == 87
    # Provenance + real data.
    assert all(r.import_batch_id and not r.is_test for r in db_session.query(SensorDeviceReading))
    assert all(r.import_batch_id and not r.is_test for r in db_session.query(MosquitoEvent))
    # Not received live from a device: no receive time, so no fake delay.
    assert all(r.received_at is None for r in db_session.query(SensorDeviceReading))
    assert all(r.received_at is None for r in db_session.query(MosquitoEvent))


# ── Dry run / all-or-nothing / validation ────────────────────────────────────

def test_dry_run_writes_nothing(client, as_user, world, db_session):
    as_user(world["admin"])
    res = post(client, "sensor-readings", sensor_csv((world["d1"], "2026-03-01T12:00:00Z", "30.5", "True")), dry_run=True)
    assert res.status_code == 200
    body = res.json()
    assert body["dry_run"] and not body["imported"]
    assert body["rows_to_import"] == 1 and body["error_count"] == 0
    assert db_session.query(SensorDeviceReading).count() == 0
    assert db_session.query(DataImport).count() == 0


def test_one_bad_row_writes_nothing_and_reports_row(client, as_user, world, db_session):
    as_user(world["admin"])
    text = sensor_csv(
        (world["d1"], "2026-03-01T12:00:00Z", "30.5", "True"),
        (world["d1"], "yesterday", "abc", "maybe"),
        (world["d1"], "2026-03-01T13:00:00Z", "nan", "False"),
    )
    res = post(client, "sensor-readings", text)
    assert res.status_code == 422
    errors = res.json()["detail"]["errors"]
    assert [e["row"] for e in errors] == [3, 4]
    assert "timestamp" in errors[0]["error"]
    assert db_session.query(SensorDeviceReading).count() == 0
    assert db_session.query(DataImport).count() == 0
    # The dry run reports the same errors with a 200.
    dry = post(client, "sensor-readings", text, dry_run=True)
    assert dry.status_code == 200 and dry.json()["error_count"] == 2


def test_empty_cells_become_null_not_zero(client, as_user, world, db_session):
    as_user(world["admin"])
    assert post(client, "sensor-readings", sensor_csv((world["d1"], "2026-03-01T12:00:00Z", "", "False"))).status_code == 200
    r = db_session.query(SensorDeviceReading).one()
    assert r.external_temperature is None and r.battery_voltage is None and r.internal_humidity is None


def test_old_file_without_optional_columns_imports_as_null(client, as_user, world, db_session):
    as_user(world["admin"])
    assert "battery_pct" not in SENSOR_HEADER and "p_mosq" not in MOSQUITO_HEADER
    assert post(client, "sensor-readings", sensor_csv((world["d1"], "2026-03-01T12:00:00Z", "1", "True"))).status_code == 200
    assert post(client, "mosquito-events", mosquito_csv((world["d1"], "2026-03-01T12:00:00Z", "Aedes sp."))).status_code == 200
    assert db_session.query(SensorDeviceReading).one().battery_pct is None
    assert db_session.query(MosquitoIndividualReading).one().p_mosq is None


def test_optional_columns_blank_is_null(client, as_user, world, db_session):
    as_user(world["admin"])
    d = world["d1"].device_uuid
    sensor = FULL_SENSOR_HEADER + f"\n,2026-03-01T12:00:00Z,{d},,,,,,,,,,,,,True,\n"
    mosquito = FULL_MOSQUITO_HEADER + f"\n,2026-03-01T12:00:00Z,{d},,,,,1,2026-03-01T12:00:00Z,,aedes,,female,\n"
    assert post(client, "sensor-readings", sensor).status_code == 200
    assert post(client, "mosquito-events", mosquito).status_code == 200
    assert db_session.query(SensorDeviceReading).one().battery_pct is None
    assert db_session.query(MosquitoIndividualReading).one().p_mosq is None


@pytest.mark.parametrize("battery, ok", [("0", True), ("100", True), ("-1", False), ("101", False), ("50.5", False), ("x", False)])
def test_battery_pct_range(client, as_user, world, db_session, battery, ok):
    as_user(world["admin"])
    text = FULL_SENSOR_HEADER + f"\n,2026-03-01T12:00:00Z,{world['d1'].device_uuid},,,,,,,,,,,,,True,{battery}\n"
    res = post(client, "sensor-readings", text)
    assert (res.status_code == 200) is ok
    if ok:
        assert db_session.query(SensorDeviceReading).one().battery_pct == int(battery)
    else:
        assert "battery_pct" in res.json()["detail"]["errors"][0]["error"]
        assert db_session.query(SensorDeviceReading).count() == 0


@pytest.mark.parametrize("p, ok", [("0", True), ("1", True), ("0.5", True), ("-0.01", False), ("1.01", False), ("87", False)])
def test_p_mosq_range(client, as_user, world, db_session, p, ok):
    as_user(world["admin"])
    d = world["d1"].device_uuid
    text = FULL_MOSQUITO_HEADER + f"\n,2026-03-01T12:00:00Z,{d},,,,,1,2026-03-01T12:00:00Z,,aedes,,female,{p}\n"
    res = post(client, "mosquito-events", text)
    assert (res.status_code == 200) is ok
    if ok:
        assert db_session.query(MosquitoIndividualReading).one().p_mosq == float(p)
    else:
        assert "p_mosq" in res.json()["detail"]["errors"][0]["error"]
        assert db_session.query(MosquitoEvent).count() == 0


def test_p_mosq_without_detection_timestamp_rejected(client, as_user, world):
    as_user(world["admin"])
    text = FULL_MOSQUITO_HEADER + f"\n,2026-03-01T12:00:00Z,{world['d1'].device_uuid},,,,,1,,,,,,0.9\n"
    res = post(client, "mosquito-events", text)
    assert res.status_code == 422 and "detection_timestamp" in res.json()["detail"]["errors"][0]["error"]


def test_timezone_offset_converted_to_utc(client, as_user, world, db_session):
    as_user(world["admin"])
    post(client, "sensor-readings", sensor_csv((world["d1"], "2026-03-01T14:00:00+02:00", "1", "ON")))
    r = db_session.query(SensorDeviceReading).one()
    assert r.timestamp == datetime(2026, 3, 1, 12, 0) and r.trap_status is True


def test_header_must_match_export(client, as_user, world):
    as_user(world["admin"])
    res = post(client, "sensor-readings", "timestamp,device_uuid,temperature\n2026-03-01T12:00:00Z,x,1\n")
    assert res.status_code == 422
    assert "temperature" in res.json()["detail"]["unknown_columns"]
    assert "trap_status" in res.json()["detail"]["missing_columns"]


def test_mosquito_detection_fields_without_detection_timestamp_rejected(client, as_user, world):
    as_user(world["admin"])
    text = MOSQUITO_HEADER + f"\n,2026-03-01T12:00:00Z,{world['d1'].device_uuid},,,,,1,,Aedes sp.,aedes,,female\n"
    res = post(client, "mosquito-events", text)
    assert res.status_code == 422 and res.json()["detail"]["errors"][0]["row"] == 2


# ── Scope ────────────────────────────────────────────────────────────────────

def test_user_forbidden(client, as_user, world):
    as_user(world["user"])
    assert post(client, "sensor-readings", sensor_csv((world["d1"], "2026-03-01T12:00:00Z", "1", "True"))).status_code == 403
    assert client.get("/imports").status_code == 403


def test_admin_cannot_import_other_cluster_device(client, as_user, world, db_session):
    as_user(world["admin"])
    res = post(client, "sensor-readings", sensor_csv(
        (world["d1"], "2026-03-01T12:00:00Z", "1", "True"),
        (world["d2"], "2026-03-01T12:00:00Z", "1", "True"),
    ))
    assert res.status_code == 422
    assert res.json()["detail"]["errors"] == [
        {"row": 3, "error": f"device_uuid: '{world['d2'].device_uuid}' is not a device in your cluster"}]
    assert db_session.query(SensorDeviceReading).count() == 0


def test_unknown_device_same_message(client, as_user, world, db_session):
    as_user(world["super"])
    text = sensor_csv((world["d1"], "2026-03-01T12:00:00Z", "1", "True")).replace(world["d1"].device_uuid, "nope")
    res = post(client, "sensor-readings", text)
    assert "'nope' is not a device in your cluster" in res.json()["detail"]["errors"][0]["error"]


def test_batch_list_and_delete_scoped(client, as_user, world, db_session):
    as_user(world["admin"])
    post(client, "sensor-readings", sensor_csv((world["d1"], "2026-03-01T12:00:00Z", "1", "True")))
    batch_id = db_session.query(DataImport).one().id
    assert [b["id"] for b in client.get("/imports").json()] == [batch_id]

    as_user(world["admin2"])
    assert client.get("/imports").json() == []
    assert client.delete(f"/imports/{batch_id}").status_code == 404
    assert db_session.query(SensorDeviceReading).count() == 1

    as_user(world["super"])
    assert [b["id"] for b in client.get("/imports").json()] == [batch_id]


# ── Duplicates ───────────────────────────────────────────────────────────────

def test_reimport_skips_exact_duplicates(client, as_user, world, db_session):
    as_user(world["admin"])
    text = sensor_csv((world["d1"], "2026-03-01T12:00:00Z", "1.5", "True"),
                      (world["d1"], "2026-03-01T13:00:00Z", "", "False"))
    assert post(client, "sensor-readings", text).json()["rows_skipped"] == 0
    # Same device + timestamp but a different value is not a duplicate.
    text2 = text + f",2026-03-01T12:00:00Z,{world['d1'].device_uuid},,,,,9.9,,,,,,,,True\n"
    body = post(client, "sensor-readings", text2).json()
    assert (body["rows_skipped"], body["rows_to_import"], body["skipped_rows"]) == (2, 1, [2, 3])
    assert db_session.query(SensorDeviceReading).count() == 3

    events = mosquito_csv((world["d1"], "2026-03-01T12:00:00Z", "Aedes sp."))
    post(client, "mosquito-events", events)
    assert post(client, "mosquito-events", events).json()["rows_skipped"] == 1
    assert db_session.query(MosquitoEvent).count() == 1
    db_session.refresh(world["d1"])
    assert world["d1"].total_mosquito_count == 1


def test_device_sent_row_with_empty_species_matches_imported_empty(client, as_user, world, db_session):
    # Ingest stores "" where the export (and import) see an empty cell → must still dedupe.
    d1 = world["d1"]
    e = MosquitoEvent(device_id=d1.id, timestamp=datetime(2026, 3, 1, 12), count=1)
    db_session.add(e)
    db_session.flush()
    db_session.add(MosquitoIndividualReading(batch_id=e.id, detection_timestamp=datetime(2026, 3, 1, 12),
                                             species="", genus="aedes", age_group="", sex="female"))
    db_session.commit()
    as_user(world["admin"])
    assert post(client, "mosquito-events", mosquito_csv((d1, "2026-03-01T12:00:00Z", ""))).json()["rows_skipped"] == 1


# ── No side effects ──────────────────────────────────────────────────────────

def test_no_notifications_and_liveness_untouched(client, as_user, world, db_session, dispatch_recorder):
    d1 = world["d1"]
    stale = datetime(2025, 1, 1)
    d1.last_activity, d1.last_sensor_data_at, d1.offline_since = stale, stale, stale
    db_session.commit()
    as_user(world["admin"])
    # Extreme temperature + trap ON + detection: each would alert on ingest.
    post(client, "sensor-readings", sensor_csv((d1, datetime.utcnow().isoformat() + "Z", "60", "True")))
    post(client, "mosquito-events", mosquito_csv((d1, datetime.utcnow().isoformat() + "Z", "Aedes sp.")))
    db_session.refresh(d1)
    assert (d1.last_activity, d1.last_sensor_data_at, d1.offline_since) == (stale, stale, stale)
    assert db_session.query(Notification).count() == 0
    assert dispatch_recorder == []


# ── Batch delete + audit ─────────────────────────────────────────────────────

def test_delete_batch_reverses_rows_and_count_and_audits(client, as_user, world, db_session):
    d1 = world["d1"]
    # A device-sent detection that must survive the batch delete.
    e = MosquitoEvent(device_id=d1.id, timestamp=T0, count=1)
    db_session.add(e)
    db_session.flush()
    db_session.add(MosquitoIndividualReading(batch_id=e.id, detection_timestamp=T0, species="x",
                                             genus="culex", age_group="", sex="male"))
    d1.total_mosquito_count = 1
    db_session.commit()

    as_user(world["admin"])
    res = post(client, "mosquito-events", mosquito_csv(
        (d1, "2026-04-01T00:00:00Z", "a"), (d1, "2026-04-02T00:00:00Z", "b"), (world["d1b"], "2026-04-02T00:00:00Z", "c")))
    batch_id = res.json()["batch_id"]
    db_session.refresh(d1)
    assert d1.total_mosquito_count == 3

    assert client.delete(f"/imports/{batch_id}").status_code == 204
    db_session.expire_all()
    assert d1.total_mosquito_count == 1
    assert world["d1b"].total_mosquito_count == 0
    assert db_session.query(MosquitoEvent).count() == 1
    assert db_session.query(MosquitoIndividualReading).one().species == "x"
    assert db_session.query(DataImport).count() == 0

    actions = [a.action for a in db_session.query(AuditLog).order_by(AuditLog.id)]
    assert actions == [AuditAction.DATA_IMPORTED, AuditAction.DATA_IMPORT_DELETED]
    deleted = db_session.query(AuditLog).filter_by(action=AuditAction.DATA_IMPORT_DELETED).one()
    assert deleted.detail["rows_deleted"] == 3 and deleted.detail["mosquito_count_reversed"] == 3
    assert deleted.actor_user_id == world["admin"].id


# ── Limits ───────────────────────────────────────────────────────────────────

def test_file_size_limit(client, as_user, world, monkeypatch):
    monkeypatch.setattr(imports, "MAX_FILE_BYTES", 100)
    as_user(world["admin"])
    assert post(client, "sensor-readings", "x" * 101).status_code == 413


def test_row_limit(client, as_user, world, db_session, monkeypatch):
    monkeypatch.setattr(imports, "MAX_ROWS", 2)
    as_user(world["admin"])
    rows = [(world["d1"], f"2026-03-01T1{i}:00:00Z", "1", "True") for i in range(3)]
    assert post(client, "sensor-readings", sensor_csv(*rows)).status_code == 413
    assert post(client, "sensor-readings", sensor_csv(*rows[:2])).status_code == 200


def test_no_data_rows(client, as_user, world):
    as_user(world["admin"])
    assert post(client, "sensor-readings", SENSOR_HEADER + "\n").status_code == 422


def test_admin_without_cluster_owns_nothing(client, as_user, world, make_user, make_device, db_session):
    loose = make_device()  # no cluster
    as_user(world["super"])
    post(client, "sensor-readings", sensor_csv((loose, "2026-03-01T12:00:00Z", "1", "True")))  # cluster_id NULL batch
    as_user(make_user(role=UserRole.ADMIN, cluster_id=None))
    assert client.get("/imports").json() == []
    assert post(client, "sensor-readings", sensor_csv((loose, "2026-03-01T13:00:00Z", "1", "True"))).status_code == 422


# ── Review fixes ─────────────────────────────────────────────────────────────

def test_device_sent_empty_age_group_round_trips_and_reimport_skips(client, as_user, world, db_session):
    d1 = world["d1"]
    e = MosquitoEvent(device_id=d1.id, timestamp=T0, count=1)
    db_session.add(e)
    db_session.flush()
    db_session.add(MosquitoIndividualReading(batch_id=e.id, detection_timestamp=T0, species="Aedes sp.",
                                             genus="aedes", age_group="", sex="female"))
    db_session.commit()
    events = export(client, db_session, world["super"], "mosquito-events")
    assert "'" not in events  # "" must export as an empty cell, not a lone quote
    wipe_rows(db_session)

    as_user(world["super"])
    assert post(client, "mosquito-events", events).json()["rows_to_import"] == 1
    assert db_session.query(MosquitoIndividualReading).one().age_group == ""
    assert post(client, "mosquito-events", events).json()["rows_skipped"] == 1


def test_lone_quote_from_old_exports_imports_as_empty(client, as_user, world, db_session):
    as_user(world["admin"])
    text = MOSQUITO_HEADER + f"\n,2026-03-01T12:00:00Z,{world['d1'].device_uuid},,,,,1,2026-03-01T12:00:00Z,x,aedes,',female\n"
    assert post(client, "mosquito-events", text).status_code == 200
    assert db_session.query(MosquitoIndividualReading).one().age_group == ""


@pytest.mark.parametrize("ts,reason", [
    ("2026-03-01", "no time of day"),
    ("2026-03-01T12:00:00", "no timezone"),
    ("2999-01-01T00:00:00Z", "in the future"),
])
def test_timestamp_needs_time_zone_and_not_future(client, as_user, world, ts, reason):
    as_user(world["admin"])
    res = post(client, "sensor-readings", sensor_csv((world["d1"], ts, "1", "True")), dry_run=True)
    assert reason in res.json()["errors"][0]["error"]


def test_count_must_be_one(client, as_user, world):
    as_user(world["admin"])
    text = mosquito_csv((world["d1"], "2026-03-01T12:00:00Z", "x")).replace(",1,2026", ",3,2026")
    assert "must be 1" in post(client, "mosquito-events", text, dry_run=True).json()["errors"][0]["error"]


def test_csv_error_is_422_and_nul_is_row_error(client, as_user, world):
    as_user(world["admin"])
    old_limit = csv.field_size_limit(10)  # process-global: restore it
    try:
        res = post(client, "sensor-readings", sensor_csv((world["d1"], "2026-03-01T12:00:00Z", "1", "True")))
    finally:
        csv.field_size_limit(old_limit)
    assert res.status_code == 422 and "could not be parsed" in res.json()["detail"]
    text = mosquito_csv((world["d1"], "2026-03-01T12:00:00Z", "a\x00b"))
    assert "NUL" in post(client, "mosquito-events", text, dry_run=True).json()["errors"][0]["error"]


def test_text_whitespace_preserved(client, as_user, world, db_session):
    as_user(world["admin"])
    post(client, "mosquito-events", mosquito_csv((world["d1"], " 2026-03-01T12:00:00Z ", " Aedes sp. ")))
    assert db_session.query(MosquitoIndividualReading).one().species == " Aedes sp. "


def test_same_time_different_values_reported_as_conflict(client, as_user, world, db_session):
    as_user(world["admin"])
    post(client, "sensor-readings", sensor_csv((world["d1"], "2026-03-01T12:00:00Z", "1", "True")))
    body = post(client, "sensor-readings", sensor_csv(
        (world["d1"], "2026-03-01T12:00:00Z", "2", "True"),
        (world["d1"], "2026-03-01T13:00:00Z", "2", "True"),
    ), dry_run=True).json()
    assert (body["rows_to_import"], body["conflict_count"], body["conflict_rows"]) == (2, 1, [2])


def _full_sensor(device, battery):
    return FULL_SENSOR_HEADER + f"\n,2026-03-01T12:00:00Z,{device.device_uuid},,,,,,,,,,,,,True,{battery}\n"


def _full_mosquito(device, p):
    ts = "2026-03-01T12:00:00Z"
    return FULL_MOSQUITO_HEADER + f"\n,{ts},{device.device_uuid},,,,,1,{ts},,aedes,,female,{p}\n"


@pytest.mark.parametrize("stored, again, skipped", [
    ("87", "87", True),    # same value: duplicate
    ("87", "", True),      # blank over a stored value: not compared, duplicate
    ("87", "50", False),   # differing value: conflict, imported
    ("", "50", False),     # value over a stored NULL: differs, conflict
])
def test_reimport_battery_pct_duplicate_or_conflict(client, as_user, world, db_session, stored, again, skipped):
    as_user(world["admin"])
    assert post(client, "sensor-readings", _full_sensor(world["d1"], stored)).status_code == 200
    body = post(client, "sensor-readings", _full_sensor(world["d1"], again)).json()
    expected = (1, 0, 0) if skipped else (0, 1, 1)
    assert (body["rows_skipped"], body["rows_to_import"], body["conflict_count"]) == expected
    assert db_session.query(SensorDeviceReading).count() == (1 if skipped else 2)


@pytest.mark.parametrize("stored, again, skipped", [
    ("0.9", "0.9", True), ("0.9", "", True), ("0.9", "0.5", False), ("", "0.5", False),
])
def test_reimport_p_mosq_duplicate_or_conflict(client, as_user, world, db_session, stored, again, skipped):
    as_user(world["admin"])
    assert post(client, "mosquito-events", _full_mosquito(world["d1"], stored)).status_code == 200
    body = post(client, "mosquito-events", _full_mosquito(world["d1"], again)).json()
    expected = (1, 0, 0) if skipped else (0, 1, 1)
    assert (body["rows_skipped"], body["rows_to_import"], body["conflict_count"]) == expected
    assert db_session.query(MosquitoIndividualReading).count() == (1 if skipped else 2)


def test_old_file_reimport_over_stored_values_is_duplicate(client, as_user, world, db_session):
    as_user(world["admin"])
    post(client, "mosquito-events", _full_mosquito(world["d1"], "0.9"))
    old = MOSQUITO_HEADER + f"\n,2026-03-01T12:00:00Z,{world['d1'].device_uuid},,,,,1,2026-03-01T12:00:00Z,,aedes,,female\n"
    body = post(client, "mosquito-events", old).json()
    assert (body["rows_skipped"], body["conflict_count"]) == (1, 0)


def test_battery_pct_whole_float_stored_as_int(client, as_user, world, db_session):
    as_user(world["admin"])
    assert post(client, "sensor-readings", _full_sensor(world["d1"], "87.0")).status_code == 200
    stored = db_session.query(SensorDeviceReading).one().battery_pct
    assert stored == 87 and isinstance(stored, int)


def test_imported_rows_not_latest_reading(client, as_user, world, db_session):
    d1 = world["d1"]
    db_session.add(SensorDeviceReading(device_id=d1.id, timestamp=T0, external_temperature=10.0))
    db_session.commit()
    as_user(world["admin"])
    post(client, "sensor-readings", sensor_csv((d1, "2026-04-01T00:00:00Z", "99", "True")))
    db_session.expire_all()
    assert d1.latest_reading.external_temperature == 10.0
