"""Read-only API smoke test against live data.

Walks every GET endpoint in the running API using real IDs pulled from the
database (every device, cluster and user), plus one call per enum value of
every enum query param (all the group_by/time-range variants), and a custom
start_date/end_date window where supported. Reports any endpoint that returns
a 5xx — i.e. any place the current data breaks the app. Nothing is written.

    uv run python -m utils.smoke_test_api
    uv run python -m utils.smoke_test_api --base-url http://127.0.0.1:8000 -v

Exit code 0 = no server errors, 1 = at least one endpoint blew up.
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import requests

from app.core.database import SessionLocal, engine
from app.authentication.models import User, ResearcherRequest  # noqa: F401  (mapper registry)
from app.authentication.enums import UserRole
from app.device.models import Device, DeviceCluster
from app.core.security.authhandler import AuthHandler

logging.basicConfig(level=logging.WARNING)
engine.echo = False  # echo=True bypasses logger levels and floods the report with SQL

MAX_IDS_PER_PARAM = 20   # cap per path-param so huge tables don't explode the run
TIMEOUT = 30


def collect_samples() -> dict:
    """Real IDs from the DB, keyed by the path-param names the routes use."""
    with SessionLocal() as session:
        devices = session.query(Device).order_by(Device.id).limit(MAX_IDS_PER_PARAM).all()
        clusters = session.query(DeviceCluster).order_by(DeviceCluster.id).limit(MAX_IDS_PER_PARAM).all()
        users = session.query(User).order_by(User.id).limit(MAX_IDS_PER_PARAM).all()
        admin = (
            session.query(User)
            .filter(User.role == UserRole.SUPER_ADMIN, User.is_active.is_(True))
            .order_by(User.id)
            .first()
        ) or (users[0] if users else None)
        if admin is None:
            sys.exit("no users in the database — cannot authenticate")
        return {
            "token_user_id": admin.id,
            "device_id": [d.id for d in devices],
            "device_uuid": [d.device_uuid for d in devices],
            "cluster_id": [c.id for c in clusters],
            "user_id": [u.id for u in users],
        }


def fill_value(schema: dict):
    """A plausible value for a non-enum query param, from its OpenAPI schema."""
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    t = schema.get("type")
    fmt = schema.get("format", "")
    if fmt == "date-time":
        return (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return "false"
    return None  # unknown/free string — leave unset unless required


def param_schema(p: dict) -> dict:
    # OpenAPI 3.1 wraps optionals in anyOf [real, null] — take the real one.
    s = p.get("schema", {})
    for candidate in s.get("anyOf", []):
        if candidate.get("type") != "null":
            return candidate
    return s


def build_calls(spec: dict, samples: dict) -> list:
    """(label, path, params) for every GET op: base call per concrete path,
    plus one call per enum value of each enum query param."""
    calls = []
    for raw_path, ops in spec["paths"].items():
        op = ops.get("get")
        if not op:
            continue
        params = op.get("parameters", [])
        path_params = [p for p in params if p["in"] == "path"]
        query_params = [p for p in params if p["in"] == "query"]

        # Concrete paths: iterate real IDs for the params we know, else "1".
        concrete = [raw_path]
        for p in path_params:
            name = p["name"]
            values = samples.get(name) or [1]
            concrete = [c.replace("{%s}" % name, str(v)) for c in concrete for v in values]

        required_fill = {}
        for p in query_params:
            if not p.get("required"):
                continue
            schema = param_schema(p)
            value = schema["enum"][0] if schema.get("enum") else fill_value(schema)
            if value is not None:
                required_fill[p["name"]] = value

        for path in concrete:
            calls.append((f"GET {path}", path, dict(required_fill)))

        # Enum sweeps and the custom date window only on the first concrete
        # path — variants multiplied across every device would explode the run.
        first = concrete[0]
        for p in query_params:
            schema = param_schema(p)
            for value in schema.get("enum") or []:
                q = dict(required_fill)
                q[p["name"]] = value
                calls.append((f"GET {first} [{p['name']}={value}]", first, q))

        names = {p["name"] for p in query_params}
        if {"start_date", "end_date"} <= names:
            q = dict(required_fill)
            q["end_date"] = datetime.now(timezone.utc).isoformat()
            q["start_date"] = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
            calls.append((f"GET {first} [custom 90-day window]", first, q))
    return calls


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every call, not just problems")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    samples = collect_samples()
    token = AuthHandler.create_access_token(samples["token_user_id"])
    headers = {"Authorization": f"Bearer {token}"}

    spec = requests.get(f"{base}/openapi.json", timeout=TIMEOUT).json()
    calls = build_calls(spec, samples)
    print(f"running {len(calls)} GET calls against {base} "
          f"(as user id {samples['token_user_id']})\n")

    failures, warnings, ok = [], [], 0
    for label, path, query in calls:
        try:
            r = requests.get(base + path, params=query, headers=headers, timeout=TIMEOUT)
            status, body = r.status_code, r.text[:300]
        except requests.RequestException as exc:
            status, body = 0, repr(exc)

        if status >= 500 or status == 0:
            failures.append((label, status, body))
            print(f"  FAIL {status:>3}  {label}\n        {body}")
        elif status >= 400:
            warnings.append((label, status, body))
            print(f"  warn {status:>3}  {label}")
        else:
            ok += 1
            if args.verbose:
                print(f"  ok   {status:>3}  {label}")

    print(f"\n{ok} ok, {len(warnings)} warnings (4xx), {len(failures)} FAILURES (5xx)")
    if warnings and not args.verbose:
        print("warnings are usually the script guessing a param wrong (422) — "
              "re-run with -v to inspect; 5xx is what indicates the data breaking the app.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
