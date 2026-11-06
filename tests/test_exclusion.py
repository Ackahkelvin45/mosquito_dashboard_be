"""Admin exclusion of a bad sensor reading / mosquito event: flag only, never
deleted or altered. Who may exclude, reason rules, include undoes, audit,
stored total consistency, analysis drops excluded rows, listings/export still
show them flagged, and the CSV import ignores the exclusion columns."""
import csv
import io
from datetime import date, datetime, timedelta

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
from app.device.models import Device, MosquitoEvent, MosquitoIndividualReading, SensorDeviceReading
from app.monitoring.data_quality import build_report as build_data_quality
from app.monitoring.model_performance import _confidence
from app.service.dashboard_service import DashboardService
from app.service.device_chart_service import DeviceChartService
from app.trap_counts.models import ManualTrapCount
from app.trap_counts.routes import compare_collections
from utils.protected_route import get_current_user, get_current_user_or_guest

NOW = datetime.utcnow().replace(microsecond=0)
TS = NOW - timedelta(hours=1)


@pytest.fixture
def app(db_session):
    from app.api_access.public_routes import router as public_router
    from app.device.routes import router as device_router
    from app.mosquito.routes import router as mosquito_router
    from app.reports.routes import router as reports_router

    application = FastAPI()
    application.include_router(device_router, prefix="/devices")
    application.include_router(mosquito_router, prefix="/mosquito")
    application.include_router(reports_router, prefix="/reports")
    application.include_router(imports.router, prefix="/imports")
    application.include_router(public_router, prefix="/api/v1")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture(autouse=True)
def export_plumbing(monkeypatch, TestingSessionLocal):
    import app.api_access.public_routes as public_routes
    monkeypatch.setattr(public_routes, "SessionLocal", TestingSessionLocal)
    api_auth._windows.clear()
    yield
    api_auth._windows.clear()


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def as_user(app):
    def _as(user):
        r = UserResponse.model_validate(user)
        app.dependency_overrides[get_current_user] = lambda: r
        app.dependency_overrides[get_current_user_or_guest] = lambda: r
    return _as


def _event(db, device, ts=TS, **reading):
    e = MosquitoEvent(device_id=device.id, timestamp=ts, count=1)
    db.add(e)
    db.flush()
    reading.setdefault("genus", "aedes")
    reading.setdefault("sex", "female")
    db.add(MosquitoIndividualReading(batch_id=e.id, detection_timestamp=ts, age_group="", **reading))
    device.total_mosquito_count = (device.total_mosquito_count or 0) + 1  # as ingest does
    db.commit()
    return e


def _reading(db, device, ts=TS, **fields):
    r = SensorDeviceReading(device_id=device.id, timestamp=ts, **fields)
    db.add(r)
    db.commit()
    return r


@pytest.fixture
def world(db_session, make_cluster, make_device, make_user):
    own, other, pub = make_cluster(), make_cluster(), make_cluster(public=True)
    d = make_device(own, region="R1", community="C1")
    dpub = make_device(pub, region="R2")
    return {
        "own": own, "d": d, "dpub": dpub,
        "good_event": _event(db_session, d, genus="aedes", p_mosq=0.9),
        "bad_event": _event(db_session, d, genus="culex", sex="male", p_mosq=0.2),
        "pub_event": _event(db_session, dpub),
        "good": _reading(db_session, d, external_temperature=25.0, external_humidity=70.0, battery_voltage=3.5),
        # Newer than "good": the bad value would show as latest if not skipped.
        "bad": _reading(db_session, d, TS + timedelta(minutes=10), external_temperature=99.0,
                        external_humidity=1.0, battery_voltage=0.5, trap_status=True),
        "admin": make_user(role=UserRole.ADMIN, cluster_id=own.id),
        "admin2": make_user(role=UserRole.ADMIN, cluster_id=other.id),
        "clusterless": make_user(role=UserRole.ADMIN),
        "user": make_user(cluster_id=own.id),
        "super": make_user(role=UserRole.SUPER_ADMIN),
    }


def _url(device, kind, row_id, action):
    return f"/devices/uuid/{device.device_uuid}/{kind}/{row_id}/{action}"


def exclude(client, device, kind, row_id, reason="sensor fault"):
    return client.patch(_url(device, kind, row_id, "exclude"), json={"reason": reason})


def include(client, device, kind, row_id):
    return client.patch(_url(device, kind, row_id, "include"))


@pytest.fixture
def excluded(client, as_user, world):
    """The bad reading and the bad event, excluded by the cluster admin."""
    as_user(world["admin"])
    assert exclude(client, world["d"], "sensor-readings", world["bad"].id).status_code == 200
    assert exclude(client, world["d"], "mosquito-events", world["bad_event"].id).status_code == 200
    return world


# ── Permissions / validation / undo / audit ──────────────────────────────────

@pytest.mark.parametrize("kind", ["sensor-readings", "mosquito-events"])
def test_who_may_exclude(client, as_user, world, db_session, kind):
    d = world["d"]
    row_id = world["bad"].id if kind == "sensor-readings" else world["bad_event"].id
    for who, code in [("admin2", 404), ("clusterless", 404), ("user", 403)]:
        as_user(world[who])
        assert exclude(client, d, kind, row_id).status_code == code, who
    # Public cluster: visible to the own-cluster admin, but not theirs to manage.
    as_user(world["admin"])
    assert exclude(client, world["dpub"], "mosquito-events", world["pub_event"].id).status_code == 404
    res = exclude(client, d, kind, row_id)
    assert res.status_code == 200
    assert res.json()["exclusion_reason"] == "sensor fault"
    assert res.json()["excluded_by_name"] == "Test User"
    assert "excluded_by_email" not in res.json()
    assert res.json()["excluded_at"]
    as_user(world["super"])
    assert include(client, d, kind, row_id).status_code == 200
    assert exclude(client, world["dpub"], "mosquito-events", world["pub_event"].id).status_code == 200


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}])
def test_reason_required(client, as_user, world, db_session, body):
    as_user(world["admin"])
    res = client.patch(_url(world["d"], "sensor-readings", world["bad"].id, "exclude"), json=body)
    assert res.status_code == 422
    db_session.refresh(world["bad"])
    assert world["bad"].excluded_at is None


def test_include_undoes_and_audits_and_409(client, as_user, world, db_session):
    d, bad = world["d"], world["bad"]
    as_user(world["admin"])
    assert exclude(client, d, "sensor-readings", bad.id, "  stuck at 99  ").status_code == 200
    db_session.refresh(bad)
    assert (bad.exclusion_reason, bad.excluded_by_user_id) == ("stuck at 99", world["admin"].id)
    assert bad.external_temperature == 99.0  # values untouched
    # Already excluded → 409, not audited.
    assert exclude(client, d, "sensor-readings", bad.id).status_code == 409
    assert include(client, d, "sensor-readings", bad.id).status_code == 200
    db_session.refresh(bad)
    assert (bad.excluded_at, bad.excluded_by_user_id, bad.exclusion_reason) == (None, None, None)
    assert include(client, d, "sensor-readings", bad.id).status_code == 409

    logs = db_session.query(AuditLog).order_by(AuditLog.id).all()
    assert [log.action for log in logs] == [AuditAction.SENSOR_READING_EXCLUDED, AuditAction.SENSOR_READING_INCLUDED]
    assert logs[0].detail == {"device_uuid": d.device_uuid, "reason": "stuck at 99"}
    assert (logs[0].actor_user_id, logs[0].target_type, logs[0].target_id) == (
        world["admin"].id, "sensor_reading", str(bad.id))

    assert exclude(client, d, "mosquito-events", world["bad_event"].id).status_code == 200
    assert include(client, d, "mosquito-events", world["bad_event"].id).status_code == 200
    assert [log.action for log in db_session.query(AuditLog).order_by(AuditLog.id)][2:] == [
        AuditAction.MOSQUITO_EVENT_EXCLUDED, AuditAction.MOSQUITO_EVENT_INCLUDED]


def test_wrong_device_or_test_row_is_404(client, as_user, world, db_session):
    as_user(world["super"])
    assert exclude(client, world["dpub"], "sensor-readings", world["bad"].id).status_code == 404
    test_row = _reading(db_session, world["d"], is_test=True)
    assert exclude(client, world["d"], "sensor-readings", test_row.id).status_code == 404


# ── Stored total ─────────────────────────────────────────────────────────────

def test_stored_count_follows_exclusion(client, as_user, world, db_session):
    d, ev = world["d"], world["bad_event"]
    as_user(world["admin"])

    def total():
        db_session.refresh(d)
        return d.total_mosquito_count

    assert total() == 2
    exclude(client, d, "mosquito-events", ev.id)
    assert total() == 1
    exclude(client, d, "mosquito-events", ev.id)  # 409: no double decrement
    assert total() == 1
    include(client, d, "mosquito-events", ev.id)
    assert total() == 2
    # Deleting an excluded event must not subtract it a second time.
    exclude(client, d, "mosquito-events", ev.id)
    assert client.delete(f"/devices/uuid/{d.device_uuid}/mosquito-events/{ev.id}").status_code == 204
    assert total() == 1


# ── Analysis leaves excluded rows out ────────────────────────────────────────

def test_dashboard_totals(excluded, db_session):
    gb = {k: "day" for k in ("totals_group_by", "chart_group_by", "gender_group_by",
                             "breakdown_group_by", "correlation_group_by")}
    res = DashboardService(db_session).get_dashboard(**gb)
    assert res.totals.total_mosquito_count == 2  # good event + public device's event
    assert res.totals.average_battery_voltage == 3.5
    assert res.gender_distribution.male == 0
    assert {i.name.lower() for i in res.breakdown.genus} == {"aedes"}
    assert {p.temperature for p in res.correlation_chart.data if p.temperature is not None} == {25.0}


def test_device_charts(excluded, db_session):
    charts = DeviceChartService(db_session).get_device_charts(excluded["d"].id, "day")
    assert sum(p.count for p in charts.mosquito_count.data) == 1
    assert {p.external for p in charts.temperature.data if p.external is not None} == {25.0}
    assert charts.sensor_status.data[-1].on_count == 0  # excluded reading said ON


def test_reports(excluded, client, as_user):
    as_user(excluded["admin"])
    r = client.get("/reports/surveillance", params={"end": date.today().isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["summary"]["detections"], body["summary"]["detection_events"]) == (2, 2)
    assert body["environment"]["temperature"]["max"] == 25.0
    assert sum(p["count"] or 0 for p in body["detections"]["daily"]) == 2


def test_surge_ignores_excluded_events(db_session, make_device, make_user):
    import app.notification.events as ev
    from app.notification.enums import NotificationType
    from app.notification.models import Notification
    make_user(role=UserRole.SUPER_ADMIN)  # a recipient, so a surge would be stored
    d = make_device()
    now = datetime.utcnow()
    for i in range(ev.NOTIFY_SURGE_THRESHOLD + 1):  # one over, but one is excluded
        db_session.add(MosquitoEvent(device_id=d.id, timestamp=now, count=1, excluded_at=now if i == 0 else None))
    db_session.commit()
    ev.emit(db_session, ev.NotificationEvent.ACTIVITY_SURGE, device=d)
    assert db_session.query(Notification).filter_by(notification_type=NotificationType.ACTIVITY_SURGE).count() == 0


def test_model_performance(excluded, db_session):
    report = _confidence(db_session, {excluded["own"].id}, NOW - timedelta(days=1), NOW)
    assert report.detections == 1
    assert report.p_mosq.mean == 0.9


def test_trap_count_comparison(excluded, db_session):
    d = excluded["d"]
    row = ManualTrapCount(device_id=d.id, period_start=NOW - timedelta(days=1), period_end=NOW,
                          genus="culex", sex="male", count=1, entered_by_user_id=excluded["admin"].id)
    db_session.add(row)
    db_session.commit()
    [col] = compare_collections(db_session, [(row, d)])
    assert (col.manual_total, col.ai_total) == (1, 0)
    assert all(g.genus != "culex" for g in col.ai_not_counted)
    assert col.device_reporting is True  # the included reading


def test_latest_reading_skips_excluded(excluded, db_session):
    d = excluded["d"]
    db_session.expire(d)
    assert d.latest_reading.id == excluded["good"].id


def test_data_quality_counts_excluded_separately(excluded, db_session):
    report = build_data_quality(db_session, {excluded["own"].id}, 1)
    [row] = report.devices
    assert (row.total_readings, row.excluded_readings) == (2, 1)
    assert (row.detections, row.excluded_detections) == (2, 1)


# ── Still listed / exported, flagged ─────────────────────────────────────────

def test_tables_list_excluded_rows_with_flags(excluded, client, as_user):
    as_user(excluded["admin"])
    d = excluded["d"]
    rows = client.get(f"/devices/uuid/{d.device_uuid}/sensor-readings").json()["items"]
    flags = {r["id"]: r["exclusion_reason"] for r in rows}
    assert flags == {excluded["bad"].id: "sensor fault", excluded["good"].id: None}
    rows = client.get("/devices/sensor-readings").json()["items"]
    assert {r["id"]: bool(r["excluded_at"]) for r in rows} == {excluded["bad"].id: True, excluded["good"].id: False}
    events = client.get(f"/devices/uuid/{d.device_uuid}/mosquito-events").json()["items"]
    assert {e["id"]: e["excluded_by_name"] for e in events} == {
        excluded["bad_event"].id: "Test User", excluded["good_event"].id: None}
    hist = {e["id"]: e for e in client.get("/mosquito").json()["items"]}
    bad = hist[excluded["bad_event"].id]
    # The FE needs the device's uuid + cluster to offer the action.
    assert (bad["device_uuid"], bad["cluster_id"]) == (d.device_uuid, d.cluster_id)
    assert {r["cluster_id"] for r in rows} == {d.cluster_id}


def test_excluder_name_only_for_managers(excluded, client, as_user, make_user, db_session):
    """Guests / public-cluster viewers see that a row is excluded and why,
    never who excluded it; the email is never sent at all."""
    d, dpub = excluded["d"], excluded["dpub"]
    as_user(excluded["super"])
    assert exclude(client, dpub, "mosquito-events", excluded["pub_event"].id).status_code == 200
    reading = _reading(db_session, dpub)
    assert exclude(client, dpub, "sensor-readings", reading.id).status_code == 200

    def names(user):
        as_user(user)
        out = {}
        for e in client.get("/mosquito").json()["items"]:
            assert "excluded_by_email" not in e
            out[("event", e["id"])] = e["excluded_by_name"]
        for r in client.get("/devices/sensor-readings").json()["items"]:
            out[("reading", r["id"])] = r["excluded_by_name"]
        for r in client.get(f"/devices/uuid/{dpub.device_uuid}/sensor-readings").json()["items"]:
            out[("dev-reading", r["id"])] = r["excluded_by_name"]
        for e in client.get(f"/devices/uuid/{dpub.device_uuid}/mosquito-events").json()["items"]:
            out[("dev-event", e["id"])] = e["excluded_by_name"]
        return out

    pub_keys = [("event", excluded["pub_event"].id), ("reading", reading.id),
                ("dev-reading", reading.id), ("dev-event", excluded["pub_event"].id)]
    # Own-cluster admin: names on own rows, none on the public cluster's rows.
    seen = names(excluded["admin"])
    assert seen[("event", excluded["bad_event"].id)] == "Test User"
    assert seen[("reading", excluded["bad"].id)] == "Test User"
    assert all(seen[k] is None for k in pub_keys)
    # A guest sees public rows flagged, but no names.
    seen = names(make_user(role=UserRole.GUEST))
    assert all(v is None for v in seen.values())
    assert seen[("event", excluded["pub_event"].id)] is None and ("event", excluded["bad_event"].id) not in seen
    # Super admin manages everything.
    assert all(names(excluded["super"])[k] == "Test User" for k in pub_keys)


def test_exclude_race_row_deleted_is_404(excluded, client, as_user, db_session, monkeypatch):
    """rowcount 0 because the row was deleted meanwhile → 404, not 409."""
    as_user(excluded["admin"])
    ev = excluded["good_event"]
    real_execute = db_session.execute

    def execute_after_delete(stmt, *a, **kw):
        if getattr(stmt, "is_update", False) and stmt.table.name == "mosquito_events":
            db_session.query(MosquitoIndividualReading).filter_by(batch_id=ev.id).delete()
            db_session.query(MosquitoEvent).filter_by(id=ev.id).delete()
            db_session.commit()  # another request's delete, already committed
        return real_execute(stmt, *a, **kw)

    monkeypatch.setattr(db_session, "execute", execute_after_delete)
    assert exclude(client, excluded["d"], "mosquito-events", ev.id).status_code == 404


def _export(client, db, owner, dataset, fmt="csv"):
    raw, prefix, key_hash = ApiKey.generate()
    db.add(ApiKey(name="k", key_prefix=prefix, key_hash=key_hash, user_id=owner.id))
    db.commit()
    res = client.get(f"/api/v1/export?dataset={dataset}&format={fmt}", headers={"X-API-Key": raw})
    assert res.status_code == 200
    return res.text


def test_export_and_import_round_trip_ignores_exclusion(excluded, client, as_user, db_session):
    for dataset, model in [("sensor-readings", SensorDeviceReading), ("mosquito-events", MosquitoEvent)]:
        text = _export(client, db_session, excluded["super"], dataset)
        rows = list(csv.DictReader(io.StringIO(text)))
        assert list(rows[0])[-2:] == ["excluded", "exclusion_reason"]
        assert sorted((r["excluded"], r["exclusion_reason"]) for r in rows if r["device_uuid"] == excluded["d"].device_uuid) == [
            ("", ""), ("True", "sensor fault")]

        # Re-import over the stored rows: excluded rows still count as stored.
        as_user(excluded["super"])
        res = client.post(f"/imports/{dataset}", content=text.encode(), headers={"Content-Type": "text/csv"})
        assert res.status_code == 200, res.text
        assert res.json()["rows_to_import"] == 0

        # Into an empty table: every row imported, none excluded.
        if model is MosquitoEvent:
            db_session.query(MosquitoIndividualReading).delete()
        db_session.query(model).delete()
        db_session.commit()
        res = client.post(f"/imports/{dataset}", content=text.encode(), headers={"Content-Type": "text/csv"})
        assert res.status_code == 200, res.text
        assert res.json()["rows_skipped"] == 0
        assert db_session.query(model).count() == len(rows)
        assert db_session.query(model).filter(model.excluded_at.isnot(None)).count() == 0


def test_jsonl_export_flag(excluded, client, db_session):
    import json
    lines = [json.loads(x) for x in _export(client, db_session, excluded["super"], "sensor-readings", "jsonl").splitlines()]
    assert {(x["excluded"], x["exclusion_reason"]) for x in lines} == {(None, None), (True, "sensor fault")}


def test_import_batch_delete_does_not_double_subtract_excluded(excluded, client, as_user, db_session):
    d = excluded["d"]
    as_user(excluded["admin"])
    ts = (NOW - timedelta(days=3)).isoformat() + "Z"
    header = ",".join(c for c in imports.EXPORT_COLUMNS[imports.MOSQUITO])
    text = header + f"\n,{ts},{d.device_uuid},,,,,1,{ts},,aedes,,female,,True,ignored\n"
    res = client.post("/imports/mosquito-events", content=text.encode(), headers={"Content-Type": "text/csv"})
    assert res.status_code == 200, res.text
    db_session.refresh(d)
    before = d.total_mosquito_count  # 1 (good) + 1 imported; the bad one was excluded
    assert before == 2
    ev = db_session.query(MosquitoEvent).filter(MosquitoEvent.import_batch_id == res.json()["batch_id"]).one()
    assert ev.excluded_at is None and ev.exclusion_reason is None
    exclude(client, d, "mosquito-events", ev.id)
    assert client.delete(f"/imports/{res.json()['batch_id']}").status_code == 204
    db_session.refresh(d)
    assert d.total_mosquito_count == 1
    assert db_session.get(Device, d.id) is not None
