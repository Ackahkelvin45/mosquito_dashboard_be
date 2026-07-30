# Notification System — API Reference

Two routers, registered in `app/core/main.py`:

- `app.include_router(notification_router, tags=["notifications"], prefix="/notifications")` — `app/notification/routes.py`
- `app.include_router(push_router, tags=["push"], prefix="/push")` — `app/notification/push_routes.py`

**Auth**: every endpoint requires `Authorization: Bearer <access_token>`
(`get_current_user`); `POST /notifications/test` additionally requires the SUPER_ADMIN
role (`require_super_admin`). Errors are the house `{"detail": "<message>"}` shape:
`401` missing/invalid token, `403` role check failed, `404` not found / not yours,
`422` validation error.

**Timestamps are naive UTC** (no `Z`, no offset — e.g. `"2026-07-30T09:12:44.123456"`).
The FE must parse them with `parseApiDate` from `lib/date.ts`, which appends `"Z"` before
`new Date()` when no zone suffix is present. Calling `new Date()` directly reads them as
browser-local time and shifts every displayed time outside UTC.

**Pagination envelope** (house `Page[T]`, used by `GET /notifications`):

```jsonc
{
  "items": [ /* NotificationResponse[] */ ],
  "total": 137,
  "page": 1,
  "page_size": 20,
  "total_pages": 7
}
```

---

## 1. Notifications

### `GET /notifications` — list (paginated)

Only the caller's rows; soft-deleted, expired, and future-scheduled rows are always
hidden.

| Param | Type | Default | Notes |
|---|---|---|---|
| `page` | int | `1` | `>= 1` |
| `page_size` | int | `20` | `1`–`100` |
| `unread_only` | bool | `false` | only `read_at IS NULL` |
| `category` | enum | — | `MOSQUITO` `SENSOR` `DEVICE` `ACCOUNT` `RESEARCH` `CLUSTER` `SYSTEM` |
| `severity` | enum | — | `INFO` `SUCCESS` `WARNING` `CRITICAL` |
| `type` | enum | — | fine-grained `NotificationType`, e.g. `LOW_BATTERY` (query alias for `notification_type`) |
| `archived` | bool | `false` | `false` = active rows only, `true` = archived rows **only** (two disjoint tabs, not a superset) |
| `search` | string | — | case-insensitive substring over title and body |
| `sort` | string | `newest` | `newest` \| `oldest` (anything else → 422) |

`200` → `Page[NotificationResponse]`:

```jsonc
{
  "items": [
    {
      "id": 412,
      "title": "Low battery on Trap-07 (3.12V)",
      "body": "Battery voltage on Trap-07 dropped to 3.12V (alert threshold 3.3V). Plan a battery replacement soon.",
      "notification_type": "LOW_BATTERY",
      "severity": "WARNING",
      "category": "SENSOR",
      "payload": { "voltage": 3.12, "device_uuid": "ESP32_007" },
      "icon": "battery-low",             // lucide icon hint, may be null
      "action_url": "/devices/7",        // FE route, may be null
      "cluster_id": 2,                   // may be null
      "device_id": 7,                    // may be null
      "read_at": null,                   // naive UTC or null
      "archived_at": null,
      "delivered_at": "2026-07-30T06:41:03.512345",
      "created_at": "2026-07-30T06:41:03.498765",
      "expires_at": null
    }
  ],
  "total": 137, "page": 1, "page_size": 20, "total_pages": 7
}
```

`NotificationResponse` never echoes `user_id` — the endpoint can only return your rows.

### `GET /notifications/unread-count`

`200` → `{"count": 3}` — unread, **unarchived**, visible rows. The FE polls this every
30 s for the bell badge.

### `PATCH /notifications/read-all`

Marks every visible unread row read (including archived ones). No body.
`200` → `{"updated": 12}`.

### `GET /notifications/preferences`

Lazily creates the row with defaults on first call. `200` →

```jsonc
{
  "species_alerts": true,
  "battery_alerts": true,
  "offline_alerts": true,
  "admin_alerts": true,
  "researcher_alerts": true,
  "email_enabled": false,     // the only default-off toggle
  "push_enabled": true,
  "in_app_enabled": true      // master switch: off = no notifications created at all
}
```

### `PUT /notifications/preferences`

Partial update — send only the toggles you want to change (all fields optional booleans;
omitted/null fields are left untouched). `200` → the full updated preference object
(same shape as GET).

```jsonc
// Request
{ "email_enabled": true, "species_alerts": false }
```

### `POST /notifications/test` — super admin only

Creates a TEST notification for the **caller**. No body.

- `201` → `NotificationResponse`
- `400` → `{"detail": "Test notification was suppressed — in-app notifications are disabled in your preferences"}`
- `403` → non-super-admin caller

### Per-notification actions

All take the integer id in the path, operate only on the caller's rows, and return the
updated `NotificationResponse` (except DELETE). A row that does not exist, is
soft-deleted, or belongs to someone else → `404 {"detail": "Notification not found"}`
(never 403 — existence is not revealed). All are idempotent.

| Method + path | Effect | Status |
|---|---|---|
| `PATCH /notifications/{id}/read` | sets `read_at` (no-op if already read) | 200 |
| `PATCH /notifications/{id}/unread` | clears `read_at` | 200 |
| `PATCH /notifications/{id}/archive` | sets `archived_at` | 200 |
| `PATCH /notifications/{id}/unarchive` | clears `archived_at` | 200 |
| `DELETE /notifications/{id}` | soft delete (sets `deleted_at`; purged after 30 days) | 204, empty body |

## 2. Push

### `GET /push/public-key`

`200` → `{"publicKey": "BOa8..."}` — the VAPID application server key, or
`{"publicKey": null}` when push is disabled server-side (VAPID env vars unset). The FE
treats `null` as "push not configured" and disables the toggle with an inline message.

### `POST /push/subscriptions`

Registers (upserts) the browser's push subscription. The upsert key is `endpoint`:
re-subscribing from the same browser refreshes the keys, reactivates the row, and resets
its failure count; an endpoint re-registered under a different account **moves** to the
new user.

```jsonc
// Request — endpoint/keys exactly as produced by PushManager.subscribe().toJSON()
{
  "endpoint": "https://fcm.googleapis.com/fcm/send/abc...",   // required, ≤500 chars
  "keys": { "p256dh": "BJf3...", "auth": "k9uT..." },          // required
  "provider": "WEBPUSH",       // optional, default WEBPUSH (FCM/APNS accepted but stub)
  "browser": "Chrome",         // optional, ≤50
  "platform": "MacIntel",      // optional, ≤50
  "device_name": "Work laptop" // optional, ≤100
}

// 201 Response
{
  "id": 5,
  "endpoint": "https://fcm.googleapis.com/fcm/send/abc...",
  "provider": "WEBPUSH",
  "browser": "Chrome",
  "platform": "MacIntel",
  "device_name": "Work laptop",
  "active": true,
  "last_used_at": null,
  "created_at": "2026-07-30T08:00:00.000000"
}
```

### `GET /push/subscriptions`

`200` → array of the caller's subscriptions (same shape as above, newest first).
Note: a bare array, not the `Page` envelope.

### `DELETE /push/subscriptions`

Takes a **JSON body** (a DELETE with a body — the endpoint is keyed on the endpoint URL,
not a path id):

```jsonc
{ "endpoint": "https://fcm.googleapis.com/fcm/send/abc..." }
```

`204` always — idempotent; deleting an unknown endpoint (or one owned by someone else)
is not an error.

## 3. curl examples

```bash
TOKEN="<access token>"
API="http://localhost:8000"

# Notifications
curl -s "$API/notifications?page=1&page_size=20&unread_only=true&severity=CRITICAL" \
  -H "Authorization: Bearer $TOKEN"
curl -s "$API/notifications/unread-count" -H "Authorization: Bearer $TOKEN"
curl -s -X PATCH "$API/notifications/read-all" -H "Authorization: Bearer $TOKEN"
curl -s -X PATCH "$API/notifications/412/read" -H "Authorization: Bearer $TOKEN"
curl -s -X DELETE "$API/notifications/412" -H "Authorization: Bearer $TOKEN" -i

# Preferences
curl -s "$API/notifications/preferences" -H "Authorization: Bearer $TOKEN"
curl -s -X PUT "$API/notifications/preferences" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email_enabled": true}'

# Test (super admin)
curl -s -X POST "$API/notifications/test" -H "Authorization: Bearer $TOKEN"

# Push
curl -s "$API/push/public-key" -H "Authorization: Bearer $TOKEN"
curl -s -X POST "$API/push/subscriptions" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"endpoint":"https://example.push/abc","keys":{"p256dh":"BJf3","auth":"k9uT"},"browser":"Chrome"}'
curl -s "$API/push/subscriptions" -H "Authorization: Bearer $TOKEN"
curl -s -X DELETE "$API/push/subscriptions" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"endpoint":"https://example.push/abc"}' -i
```

Trailing-slash URLs also work — the global path-normalisation middleware strips them
before routing (see `API_CHANGES.md` §5).
