# API & Dashboard Changes

This document describes recent backend changes: a global pagination envelope, new filters,
a chart correctness fix, a forgot-password flow, an infrastructure (CORS) fix, and the two
new dashboard charts.

> Base URLs in examples assume the API is mounted at the host root. All authenticated
> endpoints require the existing `Authorization: Bearer <token>` header unless noted.

---

## 0. All list endpoints are now paginated (breaking shape change)

Every list endpoint now returns a **paginated envelope** instead of a bare array, and
accepts `page` / `page_size` query parameters.

### New query parameters
| Param | Type | Default | Bounds | Notes |
|---|---|---|---|---|
| `page` | int | `1` | `>= 1` | 1-based page number |
| `page_size` | int | `20` | `1`–`100` | Items per page (max 100) |

### New response shape
```jsonc
{
  "items": [ /* ... the array that used to be the whole response ... */ ],
  "total": 137,        // total matching records across all pages
  "page": 1,           // current page
  "page_size": 20,     // items per page
  "total_pages": 7     // ceil(total / page_size)
}
```

### Affected endpoints
- `GET /devices`
- `GET /devices/clusters`
- `GET /devices/uuid/{device_uuid}/sensor-readings`
- `GET /devices/uuid/{device_uuid}/mosquito-events`
- `GET /mosquito`
- `GET /auth/users`
- `GET /auth/researcher-requests`

### Frontend migration
- Read the array from `response.items` (previously it was the response body itself).
- Use `total` / `total_pages` to render pagination controls.
- All existing filters still apply and are paginated **after** filtering.
- Omitting `page`/`page_size` returns the first 20 records.

### Example
```
GET /devices?region=Accra&page=2&page_size=50
```

---

## 1. `GET /mosquito` — new filters

Filter mosquito events by **region**, **device(s)**, **genus**, and **species**, in addition
to the existing date/search filters.

### New query parameters
| Param | Type | Match | Notes |
|---|---|---|---|
| `region` | string | case-insensitive partial | Matches the device's region |
| `device_uuid` | string (repeatable) | exact | Pass multiple times to filter several devices: `?device_uuid=a&device_uuid=b` |
| `genus` | string | case-insensitive partial | Matches the individual reading's genus |
| `species` | string | case-insensitive partial | Matches the individual reading's species |

### Example
```
GET /mosquito?region=Accra&device_uuid=dev-001&device_uuid=dev-002&genus=Anopheles&species=gambiae
```

### Behavior notes
- All filters are optional and combine with the existing `start_date`, `end_date`,
  `range`, and `search` parameters.
- When `genus`/`species` are used, events are matched via an outer join to the reading
  table — events **without** an individual reading are excluded for those two filters
  (expected), but remain for region/device-only filters.
- Results are de-duplicated (`DISTINCT`) and ordered by `timestamp DESC`.

---

## 2. `GET /devices` — new `trap_status` (on/off) filter

Filter devices by whether they are currently **on or off**, based on the `trap_status`
of each device's **most recent** sensor reading.

### New query parameter
| Param | Type | Notes |
|---|---|---|
| `trap_status` | boolean | `true` → devices on, `false` → devices off |

### Example
```
GET /devices?trap_status=true            # devices currently on
GET /devices?region=Accra&trap_status=false   # off devices in Accra
```

### Behavior notes
- Combines with all existing device filters (`name`, `region`, `device_uuid`,
  `min/max_mosquito_count`, `created_after`, `longitude`, `latitude`, `cluster_id`).
- "On/off" is derived from the latest reading per device (a `MAX(timestamp)` subquery
  joined back to the readings table).
- Devices with **no** sensor readings have an unknown status and are therefore excluded
  when the filter is applied. Omitting the param returns all devices (unchanged).

---

## 2b. `GET /devices` — new `is_active` field (device liveness)

Every device response now includes a computed boolean `is_active` indicating whether the
device is **communicating**, independent of its `trap_status` (on/off).

### New response field
| Field | Type | Meaning |
|---|---|---|
| `is_active` | boolean | `true` if the device reported activity within the last **24 hours**, otherwise `false` |

```jsonc
{
  "id": 1,
  "name": "Trap A",
  "last_activity": "2026-06-03T09:00:00Z",
  "trap_status": true,
  "is_active": true,          // reported within last 24h
  // ... other device fields
}
```

### Behavior notes
- Derived from each device's `last_activity` timestamp (bumped on every sensor-reading ingest).
- A device that has **never** reported, or has been silent for **> 24h**, is `is_active: false`.
- `is_active` is **not** the same as `trap_status`:
  - `trap_status` = the trap's physical on/off state, taken from the **latest** reading
    (can be stale — a dead device keeps reporting its last `trap_status`).
  - `is_active` = whether the device is **alive/communicating** right now.
  - All four combinations are valid (e.g. trap on **but** inactive = device died while on).
- The field appears on **all** device payloads: `GET /devices`, `GET /devices/{id}`,
  `GET /devices/uuid/{uuid}`, and devices nested inside cluster responses.
- The 24h window is a single constant (`ACTIVE_WINDOW_HOURS` in `app/device/schema.py`).
- This is **additive** — no existing fields changed.

---

## 3. Device charts — `mosquito_trend` is now dynamic (breaking shape change)

**Endpoint:** `GET /devices/{device_id}/charts`

### Problem fixed
The `mosquito_trend` chart previously hard-coded four series — `young_male`, `old_male`,
`young_female`, `old_female`. Real devices send other `age_group` values (e.g. `"adult"`),
so those mosquitoes were silently dropped, making the trend totals disagree with
`mosquito_count` and `mosquito_gender`.

### New response shape
`mosquito_trend` now emits a **dynamic** set of series keyed by `"{age_group}_{sex}"`,
covering whatever values actually exist in the data. The chart is lossless and always
reconciles with the count/gender totals.

```jsonc
"mosquito_trend": {
  "series_keys": ["adult_female", "adult_male", "old_male"],
  "data": [
    {
      "label": "2026-05-20",
      "timestamp": "2026-05-20T00:00:00",
      "series": { "adult_female": 2, "adult_male": 8, "old_male": 1 }
    }
    // ... one entry per time bucket
  ],
  "group_by": "month",
  "window_start": "...",
  "window_end": "..."
}
```

### Frontend migration
- Read `series_keys` to know which lines to render.
- Read each point's `series` map for values (every point carries the same keys).
- The old fixed fields (`young_male`, `old_male`, `young_female`, `old_female`) **no longer
  exist** on `mosquito_trend` points.
- The other 7 charts in this endpoint are unchanged.

---

## 4. Forgot-password flow (OTP) — new endpoints

A three-step password reset using a 6-digit OTP delivered by email. All endpoints live
under `/auth` and are unauthenticated.

### 4.1 `POST /auth/forgot-password`
Request an OTP. Always returns the same generic message (no email enumeration).

```jsonc
// Request
{ "email": "user@example.com" }

// 200 Response
{ "message": "If an account with that email exists, a verification code has been sent." }
```

### 4.2 `POST /auth/verify-otp`
Validate an OTP before showing the new-password form.

```jsonc
// Request
{ "email": "user@example.com", "otp": "123456" }

// 200 Response
{ "message": "OTP verified successfully." }

// 400 Response
{ "detail": "Invalid or expired OTP" }
```

### 4.3 `POST /auth/reset-password`
Set a new password using the OTP.

```jsonc
// Request
{ "email": "user@example.com", "otp": "123456", "new_password": "NewPass1" }

// 200 Response
{ "message": "Password reset successfully." }
```

### Rules & behavior
- OTP is **6 digits**, expires after **10 minutes**, and is stored **hashed** (same
  hashing as passwords).
- Requesting a new OTP **invalidates** any previous unused OTP for that user.
- `new_password` must satisfy the same policy as registration: ≥8 chars, at least one
  letter, one number, and one uppercase letter.
- The OTP email is sent in the background and uses the existing brand email template.

### Database migration
A new table `password_reset_otps` is added.

```bash
alembic upgrade head
```

Migration: `alembic/versions/c3d4e5f6a7b8_add_password_reset_otps.py`
(chained off the prior head `a1b2c3d4e5f6`).

---

## 5. CORS / trailing-slash fix (infrastructure)

### Problem
Requests to URLs with a trailing slash (e.g. `/auth/researcher-requests/`) were
`307`-redirected to the no-slash route. Browsers drop CORS headers across a cross-origin
redirect, which surfaced as a **"CORS error"** in the frontend.

### Fix
A path-normalization middleware strips a trailing slash **before routing**, so the
redirect never happens — for every endpoint. CORS handling is unchanged otherwise.

No action needed from clients; URLs with or without a trailing slash now resolve
identically.

---

## 6. `GET /dashboard` — two new charts

Two charts were added to the unified dashboard response. Each has its own independent
rolling-window parameter and respects the shared `region` / `cluster_id` / `device_id`
filters.

### 6.1 Correlation chart (mosquito vs temperature/humidity)

**Param:** `correlation_group_by` — `hour | day | week | month` (default `week`)

Buckets mosquito counts and average **external** temperature/humidity per time bucket,
then computes a **Pearson correlation coefficient** across buckets.

```jsonc
"correlation_chart": {
  "data": [
    {
      "label": "2026-06-01",
      "timestamp": "2026-06-01T00:00:00",
      "mosquito_count": 5,
      "temperature": 31.0,   // avg external temp in bucket; null if no readings
      "humidity": 52.0       // avg external humidity in bucket; null if no readings
    }
    // ... one entry per bucket
  ],
  "temperature_correlation": 0.9934,   // Pearson r in [-1, 1]; null if undefined
  "humidity_correlation": -0.9934,
  "group_by": "week",
  "window_start": "...",
  "window_end": "..."
}
```

**Computation details**
- Correlation is computed only over buckets where the respective sensor value exists.
- A bucket with sensor readings but **0 mosquitoes** is a valid data point and is included.
- `*_correlation` is `null` when there are fewer than 2 usable points or zero variance.
- With fine granularity (e.g. `hour` → 1-minute buckets), many zero-count buckets will
  pull the coefficient toward 0 — this is statistically honest, just expected.

### 6.2 Genus distribution heatmap

**Param:** `genus_heatmap_group_by` — `hour | day | week | month` (default `week`)

A dense `genus × time-bucket` grid of mosquito counts.

```jsonc
"genus_heatmap": {
  "genera": ["Anopheles", "Culex"],              // row axis (sorted)
  "buckets": ["2026-05-26", "2026-05-27", ...],  // column axis (ordered)
  "data": [
    {
      "genus": "Anopheles",
      "label": "2026-06-01",
      "timestamp": "2026-06-01T00:00:00",
      "count": 3
    }
    // ... one cell for EVERY genus × bucket, including zeros
  ],
  "group_by": "week",
  "window_start": "...",
  "window_end": "..."
}
```

**Computation details**
- `data` is a **complete** grid: `len(genera) × len(buckets)` cells, zeros included, so the
  frontend can render directly without filling gaps.
- Null/empty genus values are bucketed as `"Unknown"`.

### Example
```
GET /dashboard?correlation_group_by=week&genus_heatmap_group_by=month&region=Accra
```

### Verification
Both charts were verified against independently computed values:
- The Pearson implementation matches Python's stdlib `statistics.correlation` exactly.
- End-to-end runs against seeded data reproduced the expected per-bucket averages,
  coefficients, grid cells, and totals.

---

## 7. `GET /dashboard` — sensor status chart is now sampled, plus chart accuracy fixes

### 7.1 `sensor_status_chart` counts devices, not readings (semantic fix)

`trap_status` is a state, so it is now **sampled** instead of summed. Each point
samples every device's state at that instant: a device counts **once** as
`on`/`off` from its latest reading at or before the instant (carried forward
between reports). Previously each raw reading was counted, so a device
reporting every 30s outweighed one reporting hourly ~120:1.

```jsonc
"sensor_status_chart": {
  "data": [
    {
      "label": "2026-07-29",
      "timestamp": "2026-07-29T15:00:00",   // the sample instant itself
      "on_count": 3,        // devices ON at this instant (recent report)
      "off_count": 2        // devices OFF — includes devices silent > 24h
    }
  ]
}
```

- A device silent longer than 24h (`ACTIVE_WINDOW_HOURS`) counts as **off** — a
  trap that has gone dark is not operating; without this, a device that died
  while ON would stay "on" forever.
- Invariant: `on + off == devices that have reported at least once by that instant`.
- The last point samples `window_end` — i.e. the state right now.
- Response shape is unchanged (`on_count`/`off_count`); **values change**.

### 7.2 Empty filter match no longer leaks global data (bug fix)

When `region` / `cluster_id` / `device_id` matched **zero** devices, every chart
silently dropped the filter and returned whole-fleet data (totals were correct,
charts were not — they disagreed on the same screen). An empty match now returns
empty charts across all of them.

### 7.3 Trailing phantom bucket removed (bug fix)

Summed charts (`chart`, `correlation_chart`, `genus_heatmap`, and all
`/devices/{id}/charts` buckets) included a final bucket starting **at**
`window_end` that could never collect data, rendering as a false drop to zero at
the right edge. Bucket counts drop by one (e.g. week: 8 → 7).

### 7.4 `GET /devices/{id}/charts` — `sensor_status` is now sampled too

**Endpoint:** `GET /devices/{device_id}/charts` (powers the sensor detail page)

The same fix as 7.1, applied per-device. `sensor_status` previously counted raw
readings, so it plotted *how often the device reported* in on/off colours — a
device polling every 30s produced ~120 per hourly bucket, and any bucket it
skipped read as 0 even though the trap never changed state.

Each point is now the device's state sampled at that instant:

| | `on_count` | `off_count` |
|---|---|---|
| Trap ON (reported within 24h) | `1` | `0` |
| Trap OFF, **or** silent > 24h | `0` | `1` |
| Before the device's first-ever reading | `0` | `0` |

- Values are `0`/`1` — render as a **step** line, not interpolated.
- Both `0` means "state unknown", distinct from a confirmed OFF.
- Sample instants are inclusive of `window_end`, so the last point is the state
  right now (day view: 25 points, week: 8, month: 31, year: 13, hour: 61).
- The **other 7 charts on this endpoint are unchanged** — event sums and
  per-bucket averages were already correct for their data types.

### Database migration

Adds a composite index on `sensor_device_readings (device_id, timestamp)` — the
table previously had no index matching how every chart queries it.

```bash
alembic upgrade head
```

Migration: `alembic/versions/d4e5f6a7b8c9_add_sensor_reading_device_ts_index.py`

---

## 8. Authentication is now actually enforced (breaking for unauthenticated callers)

### 8.1 `Depends(security)` never validated anything

Protected routes depended on bare `HTTPBearer`, which only asserts that an
`Authorization` header **exists**. It never decoded the token, so
`Authorization: Bearer x` was accepted everywhere. All 21 such routes across
`/devices`, `/dashboard` and `/mosquito` now depend on `get_current_user`,
which verifies the JWT and resolves the user.

### 8.2 Endpoints with no auth at all

These were fully public and are now authenticated:

| Endpoint | Was |
|---|---|
| `GET /auth/users` | public — full user list, i.e. email enumeration |
| `GET /auth/users/{id}` | public |
| `GET /auth/researcher-requests` | public |
| `PATCH /auth/researcher-requests/{id}` | public |
| `PATCH /auth/researcher-requests/{id}/status` | public — anyone could approve themselves |

**`POST /auth/researcher-requests` stays public on purpose** — signup calls it
straight after registration, before the user has ever logged in.

Still public (unchanged): `POST /auth/login`, `/auth/register`,
`/auth/forgot-password`, `/auth/verify-otp`, `/auth/reset-password`,
`/auth/refresh-token`, `GET /`.

### 8.3 Refresh tokens were accepted as access tokens

`get_current_user` called `decode_token`, which ignores the `type` claim, so a
refresh token authenticated any request. It now calls `verify_token(...,
expected_type="access")`. Header parsing was also tightened — `Bearer<token>`
with no space, and a header with no space at all (which used to raise
IndexError → 500), now return 401.

### Client impact

Any caller sending a placeholder/garbage bearer token now gets **401**. The
frontend already sends real tokens and refreshes on 401, so it is unaffected.

### Not changed (deliberately)

- **Authorization (roles) is still not enforced.** Any authenticated user can
  reach every endpoint. `UserRole` and `approval_status` exist but are unused —
  enforcing them would lock out existing accounts (all current users are
  `PENDING`) and needs a product decision on who may do what.
- **Cluster passwords are still stored in plaintext**, because the approval
  email sends the password to the researcher. Hashing them requires redesigning
  that flow.

---

## 9. Minor fixes

- `app/device/routes.py`: `PATCH /{device_id}` and `DELETE /{device_id}` were
  each declared **twice**; the second pair was unreachable (FastAPI matches the
  first registration) and has been removed.
- `app/service/user_service.py`: `login_user` verified the password twice.

---

## 10. Device location comes from the device (new behaviour)

Devices now report their own position, and the region/community are derived
from it — no more hand-typed locations drifting out of sync (device 4 was
labelled "Eastern Region" while physically in Greater Accra).

### 10.1 Ingest payloads accept an optional GPS fix

`POST /devices/uuid/{uuid}/sensor-readings` and
`POST /devices/uuid/{uuid}/mosquito-events` both accept optional
`latitude` / `longitude`. The MQTT handlers accept the same, plus the common
aliases `lat` / `lon` / `lng` / `long`, and a nested
`{"location": {...}}` / `{"gps": {...}}` object.

```jsonc
{
  "timestamp": "2026-07-29T16:00:00",
  "temp_external": 30.0,
  // ... existing sensor fields ...
  "latitude": 5.5560,     // optional
  "longitude": -0.1820
}
```

Behaviour:
- **Scaled integers are normalised.** `-168572` is read as `-0.168572`
  (microdegrees). Divisors 1e6/1e5/1e4/1e3 are tried in order, matching the
  frontend's existing `normalizeCoordinate`.
- **Bad fixes are ignored, never stored**: `0,0` (a GPS with no lock),
  out-of-range values, non-numeric values, or only one of the pair.
- Movement under **50 m** is treated as GPS jitter and does not rewrite the
  position (`DEVICE_MIN_MOVE_METRES`).
- A failed geocode never costs a reading — ingestion always succeeds.

### 10.2 New `community` field, and region is now derived

`DeviceResponse` gains `community` and `location_updated_at`. Both `region` and
`community` come from reverse geocoding via **Nominatim (OpenStreetMap)** —
free, no API key.

| Env var | Default | Purpose |
|---|---|---|
| `NOMINATIM_URL` | `https://nominatim.openstreetmap.org/reverse` | endpoint |
| `NOMINATIM_USER_AGENT` | app + site URL | required by Nominatim's policy |
| `NOMINATIM_TIMEOUT` | `6` | seconds |
| `NOMINATIM_MIN_INTERVAL` | `1.0` | seconds between calls (policy: max 1/s) |
| `DEVICE_MIN_MOVE_METRES` | `50` | movement below this is jitter |
| `DEVICE_GEOCODE_RETRY_SECONDS` | `900` | retry gap for a still-unlabelled device |

Lookups run on a background thread, are cached by coordinate (~11 m buckets),
and are rate-limited per device so a device reporting every 30s cannot generate
a lookup per reading.

### 10.3 `latitude` / `longitude` / `region` are now nullable (breaking for clients)

A device can be registered before its location is known. `description` and
`gmap_link` were also made nullable — they were already `Optional` in the API
schema but `NOT NULL` in the table, so creating a device without them raised
IntegrityError (a 500) rather than being accepted.

**Clients must handle `null`** for `latitude`, `longitude`, `region`,
`community`, `description` and `gmap_link` on every device payload.

Note: range checks (`ge`/`le`) are applied on **input only**. Putting them on
the shared base model made historical out-of-range rows fail *output*
validation, 500-ing the whole device list.

### 10.4 Backfill for existing rows

```bash
uv run python -m utils.backfill_device_locations            # dry run
uv run python -m utils.backfill_device_locations --apply    # write
```

Normalises stored coordinates and fills region/community from them.

### Database migration

```bash
alembic upgrade head
```

Migration: `alembic/versions/e5f6a7b8c9d0_device_location_from_readings.py`

### Also fixed

`device_uuid` was only auto-generated when the client **sent** the field —
Pydantic skips validators for absent fields unless `validate_default=True`. A
client that omitted it got a device with `device_uuid: null`, which could never
be matched to an MQTT topic or an ingest URL.

---

## 11. Notification system — new endpoints, tables, background jobs

In-app notifications with optional browser push and email: MQTT-driven alerts (vector
species, activity surges, low battery, trap activation, extreme temperature/humidity,
sensor malfunction, unknown devices, malformed payloads), device offline/online
detection, account/researcher-flow events, and daily/weekly summaries. Full reference:
`docs/notifications/` (architecture, DB, trigger matrix, API, developer guide,
deployment).

All endpoints require `Authorization: Bearer <token>`. Timestamps are **naive UTC** —
the FE must parse them with `parseApiDate` (`lib/date.ts`), which appends `Z`.

### 11.1 `GET /notifications` — the caller's notifications (paginated)

Returns only the caller's rows (fan-out on write: one row per recipient — no visibility
filtering at read time). Standard `Page[T]` envelope.

| Param | Type | Default | Notes |
|---|---|---|---|
| `page` / `page_size` | int | `1` / `20` | `page_size` 1–100 |
| `unread_only` | bool | `false` | only unread |
| `category` | enum | — | `MOSQUITO` `SENSOR` `DEVICE` `ACCOUNT` `RESEARCH` `CLUSTER` `SYSTEM` |
| `severity` | enum | — | `INFO` `SUCCESS` `WARNING` `CRITICAL` |
| `type` | enum | — | fine-grained type, e.g. `LOW_BATTERY` |
| `archived` | bool | `false` | `true` shows archived rows **instead of** active ones |
| `search` | string | — | case-insensitive over title + body |
| `sort` | string | `newest` | `newest` \| `oldest` |

```jsonc
// 200 — items[] entries:
{
  "id": 412,
  "title": "Low battery on Trap-07 (3.12V)",
  "body": "Battery voltage on Trap-07 dropped to 3.12V ...",
  "notification_type": "LOW_BATTERY",
  "severity": "WARNING",              // INFO | SUCCESS | WARNING | CRITICAL
  "category": "SENSOR",
  "payload": { "voltage": 3.12, "device_uuid": "ESP32_007" },  // nullable
  "icon": "battery-low",              // lucide icon hint, nullable
  "action_url": "/devices/7",         // FE route to open on click, nullable
  "cluster_id": 2,                    // nullable
  "device_id": 7,                     // nullable
  "read_at": null,                    // naive UTC or null
  "archived_at": null,
  "delivered_at": "2026-07-30T06:41:03.512345",
  "created_at": "2026-07-30T06:41:03.498765",
  "expires_at": null
}
```

### 11.2 Other notification endpoints

| Method + path | Purpose | Status |
|---|---|---|
| `GET /notifications/unread-count` | `{"count": int}` — badge (FE polls every 30 s) | 200 |
| `PATCH /notifications/read-all` | mark all read → `{"updated": int}` | 200 |
| `GET /notifications/preferences` | the caller's toggles (row lazily created with defaults; `email_enabled` defaults false, everything else true) | 200 |
| `PUT /notifications/preferences` | partial update — send only changed toggles | 200 |
| `POST /notifications/test` | **super admin only** — creates a TEST notification for the caller | 201 / 400 (suppressed by prefs) / 403 |
| `PATCH /notifications/{id}/read` · `/unread` · `/archive` · `/unarchive` | per-row state; 404 (never 403) for rows that aren't yours | 200 / 404 |
| `DELETE /notifications/{id}` | soft delete (purged after 30 days) | 204 / 404 |

```jsonc
// GET/PUT /notifications/preferences — response shape
{
  "species_alerts": true, "battery_alerts": true, "offline_alerts": true,
  "admin_alerts": true, "researcher_alerts": true,
  "email_enabled": false, "push_enabled": true, "in_app_enabled": true
}
```

### 11.3 Browser push (`/push`)

| Method + path | Purpose | Status |
|---|---|---|
| `GET /push/public-key` | `{"publicKey": string \| null}` — VAPID key; `null` = push disabled server-side | 200 |
| `POST /push/subscriptions` | register/upsert this browser's subscription (upsert key: `endpoint`; re-subscribing reactivates + resets failures) | 201 |
| `GET /push/subscriptions` | the caller's subscriptions (bare array) | 200 |
| `DELETE /push/subscriptions` | remove by endpoint — **JSON body** `{"endpoint": "..."}`; idempotent | 204 |

```jsonc
// POST /push/subscriptions — request (from PushManager.subscribe().toJSON())
{
  "endpoint": "https://fcm.googleapis.com/fcm/send/abc...",
  "keys": { "p256dh": "BJf3...", "auth": "k9uT..." },
  "provider": "WEBPUSH",          // optional; FCM/APNS are registered stubs
  "browser": "Chrome", "platform": "MacIntel", "device_name": "Work laptop"  // optional
}
```

### Behavior notes

- **Recipient scoping happens at write time**: device/cluster events fan out to cluster
  members + cluster admins + super admins; admin events to super admins; account events
  to the affected user. Summaries: per-cluster recipients, super admins get one global
  summary.
- **Dedupe windows** per event type (30 min species … 24 h maintenance) collapse alert
  storms; offline/online use a state machine (`devices.offline_since`) instead.
- **Preferences gate creation**: a toggled-off alert type creates no row; `in_app_enabled`
  is the master switch. `push_enabled`/`email_enabled` gate delivery only (email
  auto-sends just for summaries and CRITICAL alerts).
- Push/email dispatch runs on a background thread and never blocks MQTT/requests;
  failed pushes are retried with backoff (max 5 attempts), and a subscription is
  deactivated after 5 consecutive failures or a 404/410 from the push service.
- Background jobs (offline detection, cleanup, push retry, summaries, health sweep) run
  on an in-process asyncio scheduler started in `lifespan`; `NOTIFY_JOBS_ENABLED=0`
  disables them. All thresholds/schedules are env-tunable with safe defaults
  (`docs/notifications/DEPLOYMENT.md`).
- Push is **silently disabled** until `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` /
  `VAPID_CLAIMS_EMAIL` are set (new dependency: `pywebpush`).

### Database migration

Four new tables — `notifications`, `push_subscriptions`, `notification_preferences`,
`notification_deliveries` — six PostgreSQL enum types, and a nullable
`devices.offline_since` column.

```bash
alembic upgrade head
```

Migration: `alembic/versions/b8c9d0e1f2a3_add_notification_tables.py` (chained off the
prior head `a7b8c9d0e1f2`). Idempotent against dev databases where `create_all` already
created the tables.

### Frontend migration

- **Bell** in the navbar (`components/notifications/NotificationBell.tsx`): unread badge
  polled every 30 s, dropdown with the 10 latest, mark-read on click, "View all".
- **Notification center** at `/notifications`: infinite scroll (first
  `useInfiniteQuery` in the codebase), all/unread/archived tabs, category/severity
  filters, debounced search, per-row read/archive/delete, preferences card with the
  push-registration flow (`hooks/usePushSubscription.ts` + `public/sw.js`).
- New shared `lib/date.ts` (`parseApiDate` appends `Z` to naive-UTC timestamps,
  `timeAgo`) — use it for **all** API timestamps in new code.
- No new FE env var: the VAPID public key always comes from `GET /push/public-key`.
- Push requires a secure context (HTTPS; `localhost` exempt). The FE Dockerfile already
  ships `public/sw.js`.

---

## Files changed (summary)

| Area | Files |
|---|---|
| Pagination envelope (all lists) | `app/core/pagination.py`, `app/device/routes.py`, `app/mosquito/routes.py`, `app/authentication/routes.py`, plus the corresponding services |
| `/mosquito` filters | `app/mosquito/routes.py`, `app/service/device_service.py`, `app/device/repository/device_repository.py` |
| `/devices` trap_status | `app/device/routes.py`, `app/service/device_service.py`, `app/device/repository/device_repository.py` |
| `/devices` is_active field | `app/device/schema.py` |
| Device chart trend fix | `app/device/chart_schema.py`, `app/service/device_chart_service.py` |
| Forgot password | `app/authentication/models.py`, `app/authentication/schema.py`, `app/authentication/routes.py`, `app/authentication/repository/userrepository.py`, `app/authentication/repository/password_reset_repository.py`, `app/service/user_service.py`, `app/service/email_service.py`, `alembic/versions/c3d4e5f6a7b8_add_password_reset_otps.py` |
| CORS fix | `app/core/main.py` |
| Notification system | `app/notification/**` (models, enums, schema, repository, service, channels/, providers/, events.py, routes.py, push_routes.py), `app/jobs/**`, trigger emits in `app/core/mqtt_client.py`, `app/service/{user,reseacher_request,device,device_cluster,device_location}_service.py`, `app/authentication/routes.py`, router + lifespan in `app/core/main.py`, `alembic/versions/b8c9d0e1f2a3_add_notification_tables.py`, `pyproject.toml` (pywebpush); FE: `components/notifications/*`, `app/(dashboard)/notifications/page.tsx`, `hooks/notification.ts`, `hooks/usePushSubscription.ts`, `queries/notification/*`, `actions/notificationMutation.ts`, `lib/date.ts`, `public/sw.js`; docs: `docs/notifications/**` |
| Dashboard charts | `app/dashboard/routes.py`, `app/dashboard/schema.py`, `app/service/dashboard_service.py` |
