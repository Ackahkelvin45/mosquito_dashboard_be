# Notification System — Deployment & Configuration

Everything is optional-with-defaults: a deployment with **zero** new env vars starts
fine — in-app notifications work, browser push is silently disabled, jobs run on their
default schedules.

---

## 1. Environment variables

All read via `os.getenv` at import time. Boolean-ish vars treat `0`, `false`, `no`, and
empty string (case-insensitive) as off; anything else is on.

### Core switches

| Var | Default | Read in | Purpose |
|---|---|---|---|
| `NOTIFY_ENABLED` | `1` | `app/notification/events.py` | Master switch for `emit()` — `0` silences every event-driven notification (the API and manual `NotificationService` calls still work) |
| `NOTIFY_JOBS_ENABLED` | `1` | `app/jobs/scheduler.py` | `0` disables the whole background-job subsystem (offline detection, cleanup, retry, summaries, health) |

### Alert thresholds (`app/notification/events.py`)

| Var | Default | Purpose |
|---|---|---|
| `NOTIFY_BATTERY_CRITICAL_V` | `3.3` | below this → LOW_BATTERY (WARNING). Escalation to CRITICAL at 3.0 V is a code constant (`BATTERY_URGENT_V`), not env-tunable |
| `NOTIFY_SURGE_THRESHOLD` | `20` | detections above this within the window → ACTIVITY_SURGE |
| `NOTIFY_SURGE_WINDOW_MIN` | `60` | surge counting window (minutes) |
| `NOTIFY_TEMP_MIN` / `NOTIFY_TEMP_MAX` | `5` / `45` | °C bounds for EXTREME_TEMPERATURE |
| `NOTIFY_HUMIDITY_MIN` / `NOTIFY_HUMIDITY_MAX` | `10` / `98` | % bounds for EXTREME_HUMIDITY |

### Job schedules (`app/jobs/*`)

| Var | Default | Purpose |
|---|---|---|
| `NOTIFY_OFFLINE_CHECK_SEC` | `120` | offline-detection job interval |
| `NOTIFY_OFFLINE_AFTER_MIN` | `30` | minutes without activity before a device counts as offline |
| `NOTIFY_CLEANUP_SEC` | `3600` | cleanup job interval (expired + soft-deleted > 30 d) |
| `NOTIFY_PUSH_RETRY_SEC` | `600` | push-retry job interval **and** the per-attempt backoff base (a delivery waits `attempts × this` between retries) |
| `NOTIFY_HEALTH_CHECK_SEC` | `86400` | device-health sweep interval |
| `NOTIFY_MAINTENANCE_DAYS` | `30` | days without activity before MAINTENANCE_DUE |
| `NOTIFY_DAILY_SUMMARY_UTC` | `07:00` | HH:MM UTC for the daily summary; the weekly summary runs Mondays at the **same** time. Invalid values fall back to 07:00 with a warning |

### Web push / VAPID (`app/notification/providers/webpush.py`)

| Var | Default | Purpose |
|---|---|---|
| `VAPID_PUBLIC_KEY` | unset | base64url application server key, served to browsers via `GET /push/public-key` |
| `VAPID_PRIVATE_KEY` | unset | base64url private key used to sign pushes |
| `VAPID_CLAIMS_EMAIL` | unset | contact email; sent as `sub: mailto:<value>` in VAPID claims |

### Email links (`app/notification/channels/email.py`)

| Var | Default | Purpose |
|---|---|---|
| `DASHBOARD_BASE_URL` | `https://mosquitosurveillancedashboard.website` | prefixed to relative `action_url`s to build the "View in Dashboard" button in notification emails |

### Frontend

No new FE env var is required. The plan mentioned an optional
`NEXT_PUBLIC_VAPID_PUBLIC_KEY`, but the shipped FE never reads it — the browser always
fetches the key from `GET /push/public-key`, so the backend env vars are the single
source of truth. The existing `NEXT_PUBLIC_BACKEND_API_URL` in `mosquito_dashboard_fe/.env`
must point at the API as before.

## 2. Generating VAPID keys

Push stays disabled until all three `VAPID_*` vars are set. Generate a key pair either
way below — both produce the same format (base64url, P-256):

**With the backend's own dependencies** (py_vapid ships with pywebpush — verified against
the pinned version):

```bash
cd mosquito_dashboard_be
uv run python -c "
from py_vapid import Vapid, b64urlencode
from cryptography.hazmat.primitives import serialization
v = Vapid(); v.generate_keys()
print('VAPID_PRIVATE_KEY=' + b64urlencode(
    v.private_key.private_numbers().private_value.to_bytes(32, 'big')))
print('VAPID_PUBLIC_KEY=' + b64urlencode(v.public_key.public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)))
"
```

**With npx** (no Python needed):

```bash
npx web-push generate-vapid-keys
```

Then set:

```bash
VAPID_PUBLIC_KEY=<public key>      # 87 chars, starts with "B"
VAPID_PRIVATE_KEY=<private key>    # 43 chars
VAPID_CLAIMS_EMAIL=admin@example.com
```

Rotating the keys invalidates existing browser subscriptions; users re-enable the push
toggle (the endpoint upsert replaces their subscription).

## 3. Database migration

```bash
cd mosquito_dashboard_be
uv run alembic upgrade head     # applies b8c9d0e1f2a3 (add notification tables)
```

Idempotent against dev databases where `create_all` already made the tables — see
[DATABASE.md](DATABASE.md) §5.

## 4. Scheduler behaviour (startup/shutdown)

From `app/core/main.py::lifespan`, in order: `create_tables()` + DB ping (fatal on
failure) → MQTT startup (fatal) → `register_all_jobs()` + `scheduler.start()`
(**non-fatal** — a scheduler failure is logged and the app stays up, unlike DB/MQTT).
On shutdown the scheduler tasks are cancelled and awaited before MQTT stops.

Operational properties:

- One asyncio task per job; start-ups staggered 5 s apart so they don't hit the DB at
  once.
- Each execution runs in `asyncio.to_thread` with its own `SessionLocal`; a job failure
  is logged and never kills its loop (30 s pause on loop-level errors).
- **Run one API instance**, or set `NOTIFY_JOBS_ENABLED=0` on all but one: the scheduler
  has no distributed lock, so N instances run every job N times. Fan-out duplication is
  partially absorbed by dedupe windows and the offline state machine, but summaries would
  duplicate.
- All schedule math is naive UTC (`datetime.utcnow`), matching the rest of the codebase.

## 5. Push requires HTTPS (secure context)

Service workers and the Push API only exist in a **secure context**: the FE must be
served over HTTPS for the push toggle to work — `usePushSubscription` checks
`window.isSecureContext` and reports "not supported" otherwise. `http://localhost` is
exempt by browser rules, so local dev works without TLS. The backend needs outbound
HTTPS to the browser push services (fcm.googleapis.com, mozilla push, etc.).

## 6. Docker notes

- **FE image ships the service worker**: `mosquito_dashboard_fe/Dockerfile` copies
  `public/` into the runner stage (`COPY --from=builder /app/public ./public`), which
  includes `public/sw.js`. No extra step needed.
- **FE build requires `.env` present**: the Dockerfile does `COPY .env .env` and `.env*`
  is gitignored — provide it at build time with at least `NEXT_PUBLIC_BACKEND_API_URL`.
- **BE env**: the backend reads its env at process start; `.env` files are not committed.
  Required additions for full functionality: the three `VAPID_*` keys (push) — everything
  else can ride the defaults. `docker-compose.yml` / your orchestrator should pass them
  as environment or env_file entries.
- CORS: the API allows `http://localhost:3000` and the production site origin only
  (see `app/core/main.py`); a new FE origin needs adding there.

## 7. What degrades when VAPID is unset

| Piece | Behaviour without VAPID |
|---|---|
| App startup | unaffected — never fails on missing push config |
| `GET /push/public-key` | returns `{"publicKey": null}` |
| FE push toggle | subscribe flow stops with the inline message "Push is not configured on the server"; the toggle never persists `push_enabled` |
| `POST /push/subscriptions` | still accepts and stores subscriptions (they just won't be delivered to) |
| Push delivery | `providers/registry.get_provider` resolves WEBPUSH to `None` (logged **once**: "Web push is disabled: VAPID_… not configured"); `PushChannel` skips every subscription, delivery rows record failure |
| In-app + email | fully functional — unaffected |

The same "resolve to `None`, log once, skip" path covers a missing/broken `pywebpush`
install — push can never take the app down.
