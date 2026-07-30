# Notification System — Developer Guide

How to extend the system: new notification types, preference flags, channels/providers,
plus testing and frontend notes.

---

## 1. Emitting a new notification type, end to end

Example: a "storage almost full" alert for a device.

**Step 1 — enum member.** Add it to *both* `NotificationType` (`app/notification/enums.py`)
and `NotificationEvent` (`app/notification/events.py`) — the two enums carry the same
values by design (`NotificationEvent` keeps `events.py` importable at trigger sites
without dragging in the model enums). Then add the value to the `notificationtype`
PostgreSQL enum in a new alembic migration (`op.execute("ALTER TYPE notificationtype ADD VALUE 'STORAGE_LOW'")`) —
the migration `b8c9d0e1f2a3` only seeds the initial 29 values.

**Step 2 — handler in `events.py`.** One function that owns the template, severity,
recipients, dedupe, icon, and action_url; register it in `_HANDLERS`:

```python
DEDUPE_STORAGE_MIN = 360  # add next to the other windows

def _storage_low(service, *, device, used_pct=None, **_):
    service.notify_cluster(
        device.cluster_id,
        title=f"Storage almost full on {device.name}",
        body=f"{device.name} reports {used_pct:.0f}% storage used.",
        notification_type=NotificationType.STORAGE_LOW,
        severity=NotificationSeverity.WARNING,
        category=NotificationCategory.DEVICE,
        payload={"used_pct": used_pct, "device_uuid": device.device_uuid},
        icon="hard-drive",                      # lucide name — see step 4
        action_url=f"/devices/{device.id}",
        device_id=device.id,
        dedupe_key=f"storage:{device.id}",
        dedupe_window_minutes=DEDUPE_STORAGE_MIN,
    )

_HANDLERS[NotificationEvent.STORAGE_LOW] = _storage_low
```

Conventions the existing handlers follow:

- Signature `def _handler(service, *, <ctx kwargs>, **_)` — keyword-only ctx, `**_`
  swallows extras so callers can pass more than you consume.
- Guard clauses first (return early when the condition isn't met — thresholds live in the
  handler, not at the call site).
- Pick a recipient resolver: `service.notify_cluster(cluster_id, ...)`,
  `service.notify_admins(...)`, or `service.notify_user(user_id, ...)`.
- Always include `device_uuid` in `payload` for device events; set `device_id` so the row
  links to the device.
- Document the ctx kwargs in the module docstring's contract table (and in
  [EVENT_FLOWS.md](EVENT_FLOWS.md) §2).

**Step 3 — call `emit` at the trigger site.** One import, one line, after the state that
the notification describes has been committed:

```python
from app.notification.events import NotificationEvent, emit
emit(db, NotificationEvent.STORAGE_LOW, device=device, used_pct=93.0)
```

`emit` never raises, so no try/except is needed at the call site. If the notification
must fire at most once per real-world episode (like offline/online), commit a state
marker first and skip the dedupe window — see the `devices.offline_since` pattern in
`app/jobs/offline_detection.py`.

**Step 4 — FE icon hint.** The FE renders `notification.icon` via the `ICON_HINTS` map in
`mosquito_dashboard_fe/components/notifications/NotificationItem.tsx`. Add the lucide
component there (`"hard-drive": <HardDrive size={16} strokeWidth={2} />`); unknown hints
fall back to the category icon (`CATEGORY_ICONS`), so this step is cosmetic, not
load-bearing.

**Step 5 — (optional) preference gate.** If users should be able to mute it, map it to a
toggle — next section.

## 2. Preference gating

The gate map lives in `app/notification/service.py`:

```python
PREFERENCE_GATES: dict[str, set[NotificationType]] = {
    "species_alerts": {SPECIES_DETECTED, ACTIVITY_SURGE},
    "battery_alerts": {LOW_BATTERY},
    "offline_alerts": {DEVICE_OFFLINE, DEVICE_ONLINE},
    "admin_alerts": {USER_REGISTERED, UNKNOWN_DEVICE, INVALID_PAYLOAD},
    "researcher_alerts": {RESEARCHER_REQUEST_SUBMITTED, _APPROVED, _REJECTED},
}
```

`_allowed_by_preferences` checks `in_app_enabled` first (master switch — off means **no
row is created at all**), then every gate whose set contains the type. Types not in any
gate (account events, cluster events, summaries, TEST) can only be silenced by
`in_app_enabled`. `push_enabled`/`email_enabled` gate channel dispatch only — the in-app
row is still created. Email additionally auto-sends only for CRITICAL severity or the
types in `EMAIL_AUTO_TYPES` (the two summaries).

**Adding a new preference flag** (e.g. `storage_alerts`):

1. `app/notification/models.py` — add the `Mapped[bool]` column (default `True`) to
   `NotificationPreference`.
2. New alembic migration — `op.add_column("notification_preferences", sa.Column("storage_alerts", sa.Boolean(), nullable=False, server_default=sa.true()))`.
3. `app/notification/schema.py` — add the field to both
   `NotificationPreferenceResponse` (required) and `NotificationPreferenceUpdate`
   (`Optional`, default `None`).
4. `app/notification/service.py` — add the entry to `PREFERENCE_GATES`.
5. FE — add a row to `ALERT_ROWS` in
   `mosquito_dashboard_fe/components/notifications/NotificationPreferences.tsx` and the
   key to the `NotificationPreferences` type in
   `queries/notification/notificationQueries.ts`. Nothing else: the card renders rows
   from that array and the partial-update mutation passes any subset through.

## 3. Adding a channel or push provider

Summarised here; full walkthrough in [ARCHITECTURE.md](ARCHITECTURE.md) §3.

- **Channel** (SMS/webhook): `DeliveryChannel` enum member (+ DB enum migration) → class in
  `channels/` implementing `NotificationChannel.send(notification, user) -> bool`
  (never raise) → `send_<channel>()` method in `NotificationService` that creates a
  `notification_deliveries` row and `mark_attempt`s it → extend the flag tuples in
  `_dispatch_channels`/`_deliver_channels` and the flag computation in `send`/`send_bulk`.
- **Push provider** (FCM/APNS): implement `send()` in the existing stub module → return a
  configured instance from `providers/registry.get_provider` (with an
  `is_<provider>_configured()` check so unconfigured still yields `None`). The
  `PushChannel` and the retry job need no changes. Return
  `PushResult(should_deactivate=True)` when the provider says the token/subscription is
  permanently gone.

## 4. Testing

Test infra (from `pyproject.toml`): dev group has `pytest`, `pytest-cov`, `httpx`;
`[tool.pytest.ini_options]` sets `testpaths = ["tests"]` with `-ra` and warning filters.
The `tests/` package sits at the backend root and is being populated per plan §8 (service
unit, events/rules, API, permissions, MQTT triggers, jobs, push provider suites).

```bash
cd mosquito_dashboard_be
uv run pytest                                          # whole suite
uv run pytest --cov=app/notification --cov=app/jobs    # with coverage
```

Patterns that matter when testing this system:

- **App without lifespan**: build the FastAPI app (or use the routers directly) without
  running `lifespan`, so tests never touch MQTT, `create_tables`, or the scheduler.
  Override `get_db` via `app.dependency_overrides` with a sqlite `StaticPool` session,
  and override `get_current_user` / `require_super_admin` per role.
- **Inline-dispatch monkeypatch**: `send`/`send_bulk` hand channel work to
  `_dispatch_channels`, which starts a daemon **thread** with its own `SessionLocal` —
  in tests that thread would race the fixture session and outlive the test. Monkeypatch
  it to run synchronously on the test session:

  ```python
  import app.notification.service as notification_service

  def test_push_dispatch(monkeypatch, session):
      def inline_dispatch(entries):
          service = notification_service.NotificationService(session)
          for notification_id, user_id, push, email in entries:
              notification = session.get(notification_service.Notification, notification_id)
              if push:
                  service.send_push(notification, user_id)
              if email:
                  service.send_email(notification, user_id)
      monkeypatch.setattr(notification_service, "_dispatch_channels", inline_dispatch)
      ...
  ```

- **Push without a network**: monkeypatch `app.notification.providers.webpush.webpush`
  (the pywebpush callable) or `providers.registry.get_provider` to a fake returning
  `PushResult(...)` — this exercises failure counting/deactivation without VAPID keys.
- **Jobs**: each `run_*` function is a plain sync callable that opens `SessionLocal` —
  point `SessionLocal` at the test engine (monkeypatch in the job module) and call it
  directly; no scheduler needed. The offline job's state machine is deterministic from
  `last_activity` / `offline_since`.
- **MQTT triggers**: call `handle_sensor_data(db, device, payload_dict)` /
  `handle_mosquito_event(...)` directly with dict payloads — no broker required.
- **Env-derived constants** (`NOTIFY_*`) are read at import time into module globals;
  monkeypatch the global (e.g. `events.NOTIFY_SURGE_THRESHOLD`), not `os.environ`.

## 5. Frontend guide

All paths under `mosquito_dashboard_fe/`.

| File | Role |
|---|---|
| `queries/notification/notificationQueries.ts` | GET fetchers + the `Notification` / `NotificationPreferences` / filter types |
| `actions/notificationMutation.ts` | mutation fetchers (PATCH/PUT/POST/DELETE) |
| `hooks/notification.ts` | TanStack Query hooks + optimistic cache helpers |
| `hooks/usePushSubscription.ts` | browser push subscribe/unsubscribe flow (SW registration, VAPID key fetch, permission handling) |
| `components/notifications/NotificationBell.tsx` | navbar bell: 30 s-polled badge, dropdown with the 10 latest (fetched only while open), mark-read on click |
| `components/notifications/NotificationItem.tsx` | row renderer: icon-hint map, severity colors, `timeAgo` timestamps |
| `components/notifications/NotificationPreferences.tsx` | the toggles card; the push toggle runs the browser flow before persisting |
| `app/(dashboard)/notifications/page.tsx` | notification center: tabs (all/unread/archived), category/severity filters, 350 ms-debounced search, infinite scroll, row actions, delete modal |
| `public/sw.js` | service worker: shows the push payload, click focuses/opens `action_url` |
| `lib/date.ts` | `parseApiDate` (appends `Z` to naive-UTC strings), `timeAgo`, `formatTimestamp` — **always** use these for API timestamps |

**Query keys** (all invalidation flows through these):

| Key | Data |
|---|---|
| `["notifications", filters, pagination]` | plain paged list (bell dropdown) |
| `["notifications", "infinite", filters]` | `useInfiniteQuery` shape (center page) |
| `["notifications", "unread-count"]` | badge count (30 s `refetchInterval` + refetch on focus) |
| `["notification-preferences"]` | preferences object |
| `["push-subscriptions"]` | subscription list |

**Optimistic updates**: every mutation hook in `hooks/notification.ts` patches the cache
in `onMutate` and invalidates in `onSettled`. The helpers to reuse:

- `transformListData` / `patchNotificationLists` — apply one item-level transform to every
  cached list under the `["notifications"]` prefix, handling both the `Paginated<T>`
  envelope and the infinite `{ pages: [...] }` shape, and leaving non-list entries (the
  unread count) untouched.
- `adjustUnreadCount(queryClient, delta | "zero")` — bump or zero the badge.

A new mutation follows the same shape: fetcher in `actions/notificationMutation.ts`,
`useMutation` in `hooks/notification.ts` with `onMutate` (patch) + `onSettled`
(invalidate).

**Adding a filter** to the center page:

1. Backend: add the query param in `routes.py::list_notifications`, thread it through
   `NotificationService.list_notifications` into `NotificationRepository.list_page`
   (filters are applied in SQL there — this repo deliberately does not use the in-memory
   `paginate()` idiom).
2. FE: add the field to `GetNotificationsFilters` and serialise it in
   `getNotifications` (`notificationQueries.ts`).
3. Add the control + a chip in `app/(dashboard)/notifications/page.tsx` (the `filters`
   memo feeds `useNotificationsInfinite`; a changed filter object restarts the infinite
   query from page 1 automatically because it is part of the query key).
