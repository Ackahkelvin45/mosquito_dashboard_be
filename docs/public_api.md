# Mosquito Dashboard — Public Data API (v1)

Read-only HTTP API for researchers and external consumers. Interactive
Swagger reference: `<BASE_URL>/docs` (tag **public-api-v1**).

`BASE_URL` is the dashboard backend, e.g. `https://api.example.org`.

## Authentication

Create an API key in the dashboard under **API Access** (any signed-in,
approved account). The raw key (`mk_…`) is shown **once** at creation — store
it securely; only a hash is kept server-side.

Send it on every request:

```
X-API-Key: mk_your_key_here
```

A key reads exactly the data its owner can see in the dashboard:

| Owner role | Key's data scope |
|---|---|
| Super admin | All clusters |
| Cluster admin / user | Their cluster + public clusters |

Scope is evaluated live per request — revoking the key, deactivating the
owner, or changing their cluster takes effect immediately.

## Endpoints

| Endpoint | Data | Pagination |
|---|---|---|
| `GET /api/v1/sensor-readings` | Environmental sensor readings (with device context) | Cursor |
| `GET /api/v1/mosquito-events` | Mosquito detections incl. species/genus/sex/age | Cursor |
| `GET /api/v1/devices` | Device metadata & location | Page |
| `GET /api/v1/clusters` | Clusters visible to your key | Page |
| `GET /api/v1/export` | Full filtered dataset as CSV / JSONL stream | — (streams) |

## Common parameters

| Parameter | Applies to | Description |
|---|---|---|
| `device_uuid` | readings, events, export | Repeatable: `?device_uuid=A&device_uuid=B` |
| `cluster_id` | all | Repeatable; intersected with your scope (out-of-scope ids yield nothing) |
| `region` | all | Repeatable region name filter |
| `start_date` / `end_date` | readings, events, export | Inclusive UTC bounds, ISO-8601 (`2026-01-31T00:00:00`) |
| `fields` | all | Comma-separated column subset, e.g. `fields=timestamp,species` |
| `order` | readings, events | `asc` or `desc` by timestamp (default `desc`) |
| `limit` | readings, events | Rows per page, 1–500 (default 100) |
| `cursor` | readings, events | Opaque `next_cursor` from the previous page |
| `page`, `page_size` | devices, clusters | Standard page pagination, `page_size` ≤ 500 |
| `sort` | devices | `created_at`, `name`, `last_activity`, `total_mosquito_count` |
| `species`, `genus` | events, export | Case-insensitive substring match |
| `sex`, `age_group` | events, export | Exact match |
| `search` | devices | Matches name, UUID, region, community |

All timestamps in responses are UTC with an explicit `Z` suffix.

## Pagination

**Cursor endpoints** (`sensor-readings`, `mosquito-events`) return:

```json
{ "items": [ … ], "next_cursor": "MjAyNi0wMy0wMVQwMDowNTowMHwxMg==" }
```

Pass `next_cursor` back as `cursor=` to fetch the next page; `null` means you
have everything. Cursors are stable under concurrent inserts — you will never
see a row twice or skip one while walking.

**Page endpoints** (`devices`, `clusters`) return the standard envelope:

```json
{ "items": [ … ], "total": 42, "page": 1, "page_size": 100, "total_pages": 1 }
```

## Examples

Walk a month of readings for two devices, temperature only:

```bash
curl -H "X-API-Key: $KEY" \
  "$BASE/api/v1/sensor-readings?device_uuid=AI4PEP_1&device_uuid=AI4PEP_2&start_date=2026-06-01T00:00:00&end_date=2026-07-01T00:00:00&fields=timestamp,device_uuid,external_temperature&order=asc&limit=500"
```

Female Aedes detections:

```bash
curl -H "X-API-Key: $KEY" \
  "$BASE/api/v1/mosquito-events?genus=aedes&sex=F"
```

Download a full year of events as CSV (streams — works for millions of rows):

```bash
curl -H "X-API-Key: $KEY" -o events_2026.csv \
  "$BASE/api/v1/export?dataset=mosquito-events&format=csv&start_date=2026-01-01T00:00:00&end_date=2027-01-01T00:00:00"
```

Python, walking every page:

```python
import requests

BASE, KEY = "https://api.example.org", "mk_..."
rows, cursor = [], None
while True:
    params = {"limit": 500, "order": "asc"}
    if cursor:
        params["cursor"] = cursor
    r = requests.get(f"{BASE}/api/v1/sensor-readings", params=params,
                     headers={"X-API-Key": KEY})
    r.raise_for_status()
    body = r.json()
    rows += body["items"]
    cursor = body["next_cursor"]
    if cursor is None:
        break
```

## Export details

- `dataset`: `sensor-readings` | `mosquito-events` | `devices`
- `format`: `csv` (default) | `jsonl` (one JSON object per line)
- Accepts the same filters as the matching list endpoint; `fields=` trims columns.
- Rows stream oldest-first; the response is chunked, so arbitrarily large
  ranges download in constant memory. `Content-Disposition` carries a
  filename with the dataset and date range.
- CSV columns are in a fixed, documented order (the `fields=` order of the
  dataset). Empty cells are NULLs. Text cells beginning with `= + - @` are
  prefixed with `'` to disarm spreadsheet formula injection.

## Rate limits

Limits are per account (owner of the key — additional keys don't add budget):

| Bucket | Limit |
|---|---|
| Query endpoints | 120 requests/min |
| Export | 5 exports/min, max 2 running at once |

Exceeding a limit returns `429` with a `Retry-After` header (seconds).

## Errors

Errors are JSON: `{ "detail": "message" }`.

| Status | Meaning |
|---|---|
| 401 | Missing/invalid/revoked/expired key, or owner no longer active/approved |
| 404 | Unknown route |
| 422 | Invalid parameter (bad date, unknown `fields`/`sort` value, bad cursor, limit > 500) |
| 429 | Rate limit or concurrent-export cap hit (see `Retry-After`) |

Requesting an out-of-scope `cluster_id` is **not** an error — it simply
yields no rows (the API never confirms whether such a cluster exists).

## Versioning

The API is versioned in the path (`/api/v1/…`). Backwards-incompatible
changes will ship as `/api/v2` with a deprecation window for v1. Additive
changes (new optional parameters, new response fields) may occur within v1 —
parse leniently.
