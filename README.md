# Mosquito Dashboard — Backend

REST API and MQTT ingester for the Mosquito Surveillance Dashboard. Field
traps publish environmental sensor readings and mosquito detections over MQTT;
this service stores them in PostgreSQL and serves them to the dashboard
frontend ([mosquito_dashboard_fe](https://github.com/Ackahkelvin45/mosquito_dashboard_fe))
and to external consumers through a read-only, API-key-protected public API.

## Stack

- Python 3.13, FastAPI, Uvicorn
- SQLAlchemy 2 + Alembic, PostgreSQL 16
- MQTT via `fastapi-mqtt` (gmqtt), Eclipse Mosquitto 2 broker
- JWT auth (PyJWT, bcrypt), email via Resend, browser push via `pywebpush`
- Dependencies managed with [uv](https://docs.astral.sh/uv/) (`pyproject.toml`, `uv.lock`)
- Tests: pytest on in-memory SQLite

## Running locally

### With Docker Compose

```bash
# create .env first (see "Environment variables" below)
docker compose up -d --build
```

This starts PostgreSQL (port 5432), Mosquitto (port 1883, anonymous access)
and the API on http://localhost:8000. The API container runs
`alembic upgrade head` and then Uvicorn. Compose overrides `DATABASE_URL`,
`MQTT_BROKER` and `MQTT_PORT` to point at its own containers.

`docker-compose.test.yml` runs a separate test stack (API on 8001, Postgres on
5433, Mosquitto on 1884) and reads `.env.test`.

### Without Docker

Needs Python 3.13, uv, a PostgreSQL database and an MQTT broker.

```bash
uv sync                                   # installs app + dev dependencies into .venv
uv run alembic upgrade head               # apply migrations
uv run uvicorn app.core.main:app --reload --port 8000
```

On startup the app connects to the database and the MQTT broker, then starts its
background jobs scheduler. If the database or the broker can't be reached, the app
exits; if the jobs scheduler fails, the error is printed and the API keeps running.

## Environment variables

Read from the environment or a `.env` file in the repo root. Never commit
`.env` (it is gitignored). Names and purpose only:

**Required** (`app/core/config.py`, the app will not start without them):

| Name | Purpose |
|---|---|
| `DATABASE_URL` | SQLAlchemy URL of the PostgreSQL database (also used by Alembic) |
| `JWT_SECRET_KEY` | Secret for signing access/refresh tokens |
| `JWT_ALGORITHM` | JWT signing algorithm (e.g. `HS256`) |
| `JWT_EXPIRATION_TIME` | Required by `Settings` (integer); not currently read elsewhere |
| `JWT_REFRESH_TOKEN_EXPIRE_SECONDS` | Refresh token lifetime |
| `RESEND_API_KEY` | Resend API key for outgoing email |
| `EMAIL_FROM` | Sender address for outgoing email |
| `MQTT_BROKER` | MQTT broker host |
| `MQTT_PORT` | MQTT broker port |
| `TOPIC_SENSOR_DATA` | MQTT subscription for sensor readings (see below) |
| `TOPIC_MOSQUITO_COUNT` | MQTT subscription for mosquito detections (see below) |
| `MQTT_CLIENT_ID` | MQTT client id of this service |

**Optional** (have defaults):

| Name | Purpose |
|---|---|
| `CORS_ORIGINS` | Comma-separated allowed origins (default includes `http://localhost:3000`) |
| `SQL_ECHO` | Log every SQL statement |
| `JWT_ACCESS_TOKEN_EXPIRE_SECONDS` | Access token lifetime (default 1800) |
| `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_CLAIMS_EMAIL` | Web push (VAPID) keys; browser push is only configured when all three are set |
| `DASHBOARD_BASE_URL` | Frontend URL used for links in emails |
| `NOTIFY_*`, `MONITOR_*`, `AUDIT_RETENTION_DAYS` | Alert thresholds, job intervals and retention periods |
| `NOMINATIM_URL`, `NOMINATIM_USER_AGENT`, `NOMINATIM_TIMEOUT`, `NOMINATIM_MIN_INTERVAL` | Reverse geocoding of device positions |
| `DEVICE_MIN_MOVE_METRES`, `DEVICE_GEOCODE_RETRY_SECONDS` | Device relocation / geocode retry tuning |

The notification-related variables are documented in
[docs/notifications/DEPLOYMENT.md](docs/notifications/DEPLOYMENT.md).

## Tests

```bash
uv run pytest -q
```

Tests use in-memory SQLite and `tests/conftest.py` sets defaults for every
required setting, so no `.env`, database or broker is needed. CI
(`.github/workflows/ci.yml`) runs the same command on every push and pull
request.

## Database migrations

Migrations live in `alembic/versions/`. Alembic reads `DATABASE_URL` from the
environment / `.env`.

```bash
uv run alembic upgrade head                              # apply
uv run alembic revision --autogenerate -m "describe change"  # create
```

The Docker image applies `alembic upgrade head` on every container start.

## API documentation

- Interactive Swagger UI: `http://localhost:8000/docs` (FastAPI default)
- Public data API (API-key access, `/api/v1/...`): [docs/public_api.md](docs/public_api.md)
- Notification system: [docs/notifications/README.md](docs/notifications/README.md)

## MQTT topics

The service subscribes to `TOPIC_SENSOR_DATA` and `TOPIC_MOSQUITO_COUNT`, plus
the same two with a `_test` suffix. Topics are parsed as
`mosquito_dashboard/<device_uuid>/<event_type>`, so the expected values are:

| Topic | Payload |
|---|---|
| `mosquito_dashboard/+/sensor_data` | Environmental reading (temperature, humidity, pressure, battery, trap status, optional GPS fix) |
| `mosquito_dashboard/+/mosquito_data` | Batch of mosquito detections (species, genus, sex, model confidence, ...) |
| `mosquito_dashboard/+/sensor_data_test`, `mosquito_dashboard/+/mosquito_data_test` | Same payloads from a device in Test mode: stored and marked as test; they do not trigger notifications or add to the device's mosquito total |

The `<device_uuid>` must belong to a registered device. Messages from unknown
devices are not stored as readings; they are recorded as sightings so the
dashboard can offer to register the device. Payload handling is in
`app/core/mqtt_client.py`.

## Frontend connection

The frontend calls this API over HTTP using `NEXT_PUBLIC_BACKEND_API_URL`
(e.g. `http://localhost:8000` locally) with a JWT bearer token. The
frontend's origin must be listed in `CORS_ORIGINS`.

## Licence

To be decided.
