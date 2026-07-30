# Notification System — Documentation

In-app, browser-push, and email notifications for the Mosquito Dashboard: MQTT-driven
alerts (species, battery, environment), device offline/online detection, account and
researcher-flow events, and daily/weekly summaries. Design doc: `../../NOTIFICATIONS_PLAN.md`
(these pages describe the code as shipped, including deviations from the plan).

| Doc | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Component diagram, design principles (fan-out on write, never-raise emit, dedupe, preference gating), channel/provider abstraction, RBAC scoping, deviations from the plan |
| [DATABASE.md](DATABASE.md) | ER diagram, column/index reference for the four tables + `devices.offline_since`, soft-delete/expiry/scheduling semantics, migration `b8c9d0e1f2a3` |
| [EVENT_FLOWS.md](EVENT_FLOWS.md) | The full trigger matrix (event → source → recipients → severity → dedupe → icon), the `emit()` ctx contract, sequence diagrams, background-job reference |
| [API.md](API.md) | Every `/notifications` and `/push` endpoint: params, response shapes, status codes, the naive-UTC timestamp rule, curl examples |
| [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) | Adding a notification type / preference flag / channel / provider, testing patterns, FE hooks and cache helpers |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Env var table (`NOTIFY_*`, `VAPID_*`), VAPID key generation, scheduler ops, HTTPS requirement, Docker notes, degradation without VAPID |

## Quickstart

```bash
cd mosquito_dashboard_be
uv run alembic upgrade head                    # tables + devices.offline_since
npx web-push generate-vapid-keys               # optional — enables browser push
export VAPID_PUBLIC_KEY=... VAPID_PRIVATE_KEY=... VAPID_CLAIMS_EMAIL=you@example.com
uv run uvicorn app.core.main:app --reload      # scheduler + jobs start with the app
cd ../mosquito_dashboard_fe && npm run dev     # FE on localhost:3000
```

Log in, click the bell in the navbar (badge polls every 30 s), open `/notifications` for
the full center + preferences, and toggle **Push** there to register this browser. As a
super admin, `POST /notifications/test` creates a test notification for yourself.
