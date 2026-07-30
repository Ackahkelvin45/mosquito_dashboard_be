# Notification System — Implementation Plan

Status: **IN PROGRESS** (plan authored 2026-07-30)

This document is the single source of truth for the notification feature: architecture,
database design, API contract, trigger matrix, background jobs, RBAC scoping, and the
frontend deliverables. It follows existing house conventions exactly (see
`RBAC_PLAN.md`, `API_CHANGES.md`, and the conventions notes at the bottom).

---

## 1. Architecture overview

```
                    ┌────────────────────────────────────────────────┐
                    │              Trigger sources                   │
                    │  MQTT handlers · auth/researcher/cluster flows │
                    │  background jobs (offline, summaries, health)  │
                    └───────────────────┬────────────────────────────┘
                                        │ emit(event, ctx)   (never raises)
                                        ▼
                    ┌────────────────────────────────────────────────┐
                    │        app/notification/events.py              │
                    │  event → rule: recipients, severity, dedupe    │
                    └───────────────────┬────────────────────────────┘
                                        │ NotificationService.send()/send_bulk()
                                        ▼
                    ┌────────────────────────────────────────────────┐
                    │     app/notification/service.py                │
                    │  single entry point; fan-out on write:         │
                    │  one Notification row per recipient            │
                    └───────┬───────────────┬────────────────────────┘
                            │               │ channel dispatch (respects
                            │               │ NotificationPreference)
                            ▼               ▼
                   in-app (DB row)   app/notification/channels/
                                      ├─ push.py  → providers/ (webpush | fcm | apns)
                                      ├─ email.py → existing email_service (wired, minimal)
                                      └─ (future: sms.py, webhook.py)
```

Principles:

- **Business logic never constructs `Notification` objects.** It calls
  `emit(NotificationEvent.X, **ctx)` (one import, one line, wrapped so it can
  never raise) or, for imperative cases, `NotificationService` methods.
- **Fan-out on write**: recipients are resolved at emit time; each recipient gets
  their own row. Reads are therefore trivially scoped (`user_id = me`) — no
  visibility joins at read time, no leakage risk.
- **Channels are pluggable.** `NotificationChannel` ABC; in-app is the DB row
  itself; push goes through a `PushProvider` ABC (`webpush` implemented,
  `fcm`/`apns` stubs) selected per subscription. Email reuses the existing
  Resend module. SMS/webhook slot in later without touching call sites.
- **Delivery tracking**: `notification_deliveries` rows per (notification,
  channel) record attempts/status/error → enables the push retry job and future
  email/webhook retries.
- **Push dispatch is off-request**: daemon thread (house pattern from
  `device_location_service._dispatch_geocode`) with its own `SessionLocal()`.
  MQTT ingestion is never blocked.
- **Dedupe/throttle**: every rule computes a `dedupe_key`
  (e.g. `low_battery:device:42`) and a throttle window; `send()` skips creation
  if an identical key exists inside the window.

## 2. Database design (migration `b8c9d0e1f2a3`, revises `a7b8c9d0e1f2`)

### notifications
| column | type | notes |
|---|---|---|
| id | int PK | |
| user_id | FK users.id, indexed, NOT NULL | recipient |
| cluster_id | FK device_clusters.id, nullable, indexed | scope context |
| device_id | FK devices.id, nullable, indexed | source device |
| title | varchar(200) | |
| body | varchar(1000) | |
| notification_type | enum NotificationType | fine-grained (see §4) |
| severity | enum NotificationSeverity | INFO / SUCCESS / WARNING / CRITICAL |
| category | enum NotificationCategory | MOSQUITO / SENSOR / DEVICE / ACCOUNT / RESEARCH / CLUSTER / SYSTEM |
| payload | JSON, nullable | structured context (species, voltage, uuid…) |
| icon | varchar(50), nullable | lucide icon name hint for FE |
| action_url | varchar(255), nullable | FE route, e.g. `/devices/42` |
| dedupe_key | varchar(255), nullable, indexed | throttling |
| read_at / delivered_at / archived_at / deleted_at | DateTime nullable | soft delete via deleted_at |
| scheduled_for | DateTime nullable | future scheduling; rows with `scheduled_for > now` are hidden until due |
| expires_at | DateTime nullable | cleanup job purges after expiry |
| created_at | DateTime default utcnow | naive UTC (house convention on the event path) |

Composite indexes: `(user_id, created_at desc)`, `(user_id, read_at)`, `(dedupe_key, created_at)`.

### push_subscriptions
user_id FK indexed · endpoint varchar(500) unique · p256dh varchar(255) nullable ·
auth varchar(255) nullable · provider enum (WEBPUSH/FCM/APNS) default WEBPUSH ·
browser varchar(50) · platform varchar(50) · device_name varchar(100) ·
active bool default true · failure_count int default 0 · last_used_at ·
created_at · updated_at. Multiple rows per user (one per browser/device).

### notification_preferences
user_id FK unique · species_alerts · battery_alerts · offline_alerts ·
admin_alerts · researcher_alerts · email_enabled · push_enabled ·
in_app_enabled (all bool; defaults true except email_enabled false) ·
created_at · updated_at. Lazily created with defaults on first read.

### notification_deliveries
notification_id FK indexed · channel enum (PUSH/EMAIL) · status enum
(PENDING/SENT/FAILED) · attempts int · last_attempt_at · error varchar(500).

### devices.offline_since (new nullable column)
State marker for the offline/online detector — set when the offline job first
flags the device, cleared (with an ONLINE notification) when activity resumes.

## 3. RBAC scoping rules (fan-out targets)

Resolved at **write** time; reads are always `WHERE user_id = current_user.id AND deleted_at IS NULL`.

| Event scope | Recipients |
|---|---|
| Device/cluster events (species, battery, offline, …) | users with `cluster_id = device.cluster_id` (members+admins) ∪ all SUPER_ADMINs. Device without cluster → SUPER_ADMINs only. Public clusters do **not** broadcast to everyone (would be spam) — members + super admins only. |
| Admin/system events (unknown device, malformed payloads, registrations, researcher requests) | SUPER_ADMINs (approval rights are super-admin-only per RBAC_PLAN) |
| Account events (approval, rejection, role change, password reset) | the affected user |
| Summaries | per-user, driven by their preferences |

No cross-cluster leakage is possible by construction; permission tests assert it.

## 4. Trigger matrix

| Source | Event → NotificationType | Severity | Dedupe window |
|---|---|---|---|
| MQTT mosquito | vector/dangerous species (genus Anopheles, Aedes, Culex list via `VECTOR_SPECIES` map) → SPECIES_DETECTED; adult female of vector species escalates | CRITICAL (vector ♀ adult) / WARNING | 30 min per device+species |
| MQTT mosquito | count surge: > `NOTIFY_SURGE_THRESHOLD` (default 20) detections in `NOTIFY_SURGE_WINDOW_MIN` (60) per device → ACTIVITY_SURGE | WARNING | 2 h per device |
| MQTT sensor | battery < `NOTIFY_BATTERY_CRITICAL_V` (3.3) → LOW_BATTERY | WARNING/CRITICAL (<3.0) | 6 h per device |
| MQTT sensor | trap_status flips false→true → TRAP_TRIGGERED | INFO | 1 h per device |
| MQTT sensor | temp/humidity outside bounds → EXTREME_TEMPERATURE / EXTREME_HUMIDITY; nulls/implausible values → SENSOR_MALFUNCTION | WARNING | 3 h per device |
| MQTT handler | unknown device_uuid → UNKNOWN_DEVICE (absorbs TODO.md item, minimal form) · malformed topic/JSON → INVALID_PAYLOAD | WARNING | 1 h per uuid / topic |
| Offline job | last_activity stale > `NOTIFY_OFFLINE_AFTER_MIN` (30) → DEVICE_OFFLINE; recovery → DEVICE_ONLINE | CRITICAL / SUCCESS | state-machine via `devices.offline_since` |
| Location service | GPS moved > threshold → DEVICE_LOCATION_CHANGED | WARNING | 12 h per device |
| Auth flows | register → USER_REGISTERED (to super admins) · approve/reject → USER_APPROVED/USER_REJECTED (to user) · role change → ROLE_CHANGED · password reset → PASSWORD_RESET | INFO | — |
| Researcher flows | submitted (→ super admins) / approved / rejected (→ requester) | INFO | — |
| Cluster flows | created/updated/device added/removed/reassigned | INFO | — |
| Jobs | daily/weekly summary → DAILY_SUMMARY / WEEKLY_SUMMARY · maintenance overdue (no activity 30 d) → MAINTENANCE_DUE | INFO | daily/weekly |
| Manual | POST /notifications/test → TEST | INFO | — |

## 5. Background jobs — `app/jobs/`

No scheduler exists in the codebase; we add a **dependency-free asyncio interval
scheduler** (`app/jobs/scheduler.py`, ~100 lines): started in `lifespan` after
`mqtt.mqtt_startup()`, cancelled on shutdown; each job runs sync code via
`asyncio.to_thread` with its own `SessionLocal()` (house pattern), wrapped in
blanket `except Exception: logger.exception` so a job can never take the app
down. Supports fixed intervals and daily-at-HH:MM. Env-tunable via `NOTIFY_*`
vars with defaults (never breaks startup).

Jobs: offline detection (2 min) · notification cleanup: expired + soft-deleted
> 30 d (1 h) · push retry: FAILED deliveries, backoff, deactivate subscription
after 5 consecutive failures (10 min) · daily summary 07:00 UTC · weekly
summary Mon 07:00 · maintenance/health sweep (24 h).

## 6. API contract (FE builds against this verbatim)

All under auth (`Bearer`), errors as `{"detail": str}`, list envelope is the house
`Page[T]` (`items,total,page,page_size,total_pages`). Timestamps are naive UTC —
**FE must append `Z` before `new Date()`** (known convention).

```
GET    /notifications                 page,page_size, unread_only?,category?,severity?,
                                      type?,archived?(default false),search?,sort?=newest|oldest
GET    /notifications/unread-count    → {"count": int}
PATCH  /notifications/read-all        → {"updated": int}            (before /{id} routes)
GET    /notifications/preferences     → NotificationPreferenceResponse
PUT    /notifications/preferences     partial body → updated response
POST   /notifications/test            super-admin only → creates a TEST notification for caller
PATCH  /notifications/{id}/read       → NotificationResponse
PATCH  /notifications/{id}/unread     → NotificationResponse
PATCH  /notifications/{id}/archive    → NotificationResponse
PATCH  /notifications/{id}/unarchive  → NotificationResponse
DELETE /notifications/{id}            soft delete → 204

GET    /push/public-key               → {"publicKey": str|null}     (VAPID; null = push disabled)
POST   /push/subscriptions            {endpoint,keys{p256dh,auth},provider?,browser?,platform?,device_name?}
                                      → PushSubscriptionResponse (upsert on endpoint)
GET    /push/subscriptions            → list of caller's subscriptions
DELETE /push/subscriptions            {endpoint} → 204 (idempotent)
```

`NotificationResponse`: id, title, body, notification_type, severity, category,
payload, icon, action_url, cluster_id, device_id, read_at, archived_at,
delivered_at, created_at, expires_at. (No user_id echo needed; never exposes
other users' rows.)

Router registration in `app/core/main.py`:
`app.include_router(notification_router, tags=["notifications"], prefix="/notifications")`
`app.include_router(push_router, tags=["push"], prefix="/push")`

## 7. Frontend deliverables (Next.js 16 / React 19 / Tailwind v4 / TanStack Query / Zustand / lucide)

- **Bell** in the dashboard header: unread badge (count from
  `/notifications/unread-count`, polled every 30 s), animated dropdown with the
  10 latest, mark-read on click, "View all" link. Escape/outside-click close,
  `aria-*` complete.
- **Notification center** page `/notifications`: infinite scroll (TanStack
  `useInfiniteQuery` over `page`), filters (category, severity, unread,
  archived), search, mark all read, per-item read/unread, archive, delete,
  severity colors + category icons (lucide), relative timestamps (append `Z`!),
  empty states, loading skeletons (react-loading-skeleton), responsive, dark
  mode via existing token strategy.
- **Preferences** UI (toggles) on the notifications page (tab or section).
- **Push registration**: `public/sw.js` service worker + a settings toggle that
  runs the `PushManager.subscribe` flow against `/push/public-key`, registers via
  `/push/subscriptions`. Graceful when push is disabled server-side (null key)
  or permission denied. FE env var: `NEXT_PUBLIC_VAPID_PUBLIC_KEY` optional
  (server key endpoint is the source of truth).

Design-system decisions (deviations from the original spec, deliberate):
- **No `dark:` variants** — the app has zero dark-mode support (no toggle, no
  tokens, no `dark:` usage). Matching the design system exactly wins over the
  spec bullet; dark mode is a separate future feature.
- Infinite scroll uses TanStack `useInfiniteQuery` — first use in the codebase
  (existing lists are page-number based) — explicitly required by the spec.
- No toast library exists; transient feedback uses the house inline-banner
  pattern, not a new dependency.
- New shared `lib/date.ts` lifts the one correct naive-UTC parser
  (`parseApiDate` from `map/sensor/[id]/page.tsx`) and adds `timeAgo()`.

## 8. Testing (new infra — none exists today)

`uv add --dev pytest pytest-cov httpx` · `tests/` package at BE root with
sqlite-backed session fixture (StaticPool), app fixture without lifespan
(no MQTT/DB-startup), `dependency_overrides[get_db]` + auth override helpers
per role. Suites: service unit (send/dedupe/fan-out/mark/cleanup), events/rules,
API (all endpoints, pagination, filters), permissions (cross-cluster isolation,
role gates), MQTT trigger integration (calling handlers directly with fake
payloads), jobs (offline state machine, cleanup, retry, summaries), push
provider (mocked webpush), concurrency (parallel mark-read / dedupe race).
Target ≥90% coverage of `app/notification/` + `app/jobs/`.

## 9. Config additions (all optional, `os.getenv` with defaults — startup-safe)

`NOTIFY_ENABLED=1`, `NOTIFY_BATTERY_CRITICAL_V=3.3`, `NOTIFY_SURGE_THRESHOLD=20`,
`NOTIFY_SURGE_WINDOW_MIN=60`, `NOTIFY_OFFLINE_AFTER_MIN=30`,
`NOTIFY_TEMP_MIN/MAX=5/45`, `NOTIFY_HUMIDITY_MIN/MAX=10/98`,
`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_CLAIMS_EMAIL` (push silently
disabled when unset). New dep: `pywebpush`.

## 10. File ownership map (parallel agents — do not cross)

| Agent | Owns |
|---|---|
| Backend core | `app/notification/**` (models, enums, schema, repository, service, channels/, providers/, events.py, routes.py, push_routes.py), migration `b8c9d0e1f2a3`, `app/core/main.py` router lines, `pyproject.toml` |
| Triggers | `app/core/mqtt_client.py`, `app/authentication/routes.py`, `app/service/user_service.py`, `app/service/reseacher_request_service.py`, `app/service/device_cluster_service.py`, `app/service/device_service.py`, `app/service/device_location_service.py` |
| Jobs | `app/jobs/**`, `lifespan` block in `app/core/main.py` |
| Frontend | `mosquito_dashboard_fe/**` notification files |
| Tests | `tests/**`, pytest config |
| Docs | `docs/notifications/**`, `API_CHANGES.md` section |

Sequencing: Backend core ∥ Frontend → then Triggers ∥ Jobs → then Tests ∥ Security/Perf review → docs + validation.
