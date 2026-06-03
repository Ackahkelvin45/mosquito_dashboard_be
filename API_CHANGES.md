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
| Dashboard charts | `app/dashboard/routes.py`, `app/dashboard/schema.py`, `app/service/dashboard_service.py` |
