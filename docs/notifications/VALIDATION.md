# Notification System — Final Validation Report

Date: 2026-07-30. All checks run against the final state of both working trees
(uncommitted; nothing was committed by this work).

## Test results

| Check | Command | Result |
|---|---|---|
| Backend test suite | `uv run pytest tests/ -q --cov=app/notification --cov=app/jobs` | **309 passed, 0 failed** (~6 s) |
| Coverage | same, `--cov-report=term` | **100%** — 1311/1311 statements across every file in `app/notification/` and `app/jobs/` (target was ≥90%) |
| Backend import | `uv run python -c "from app.core.main import app"` | clean; all 15 notification/push routes registered |
| Bytecode compile | `uv run python -m compileall app -q` | clean |
| Alembic | `uv run alembic heads` | single head `c9d0e1f2a3b4` (chain: `a7b8c9d0e1f2` → `b8c9d0e1f2a3` notification tables → `c9d0e1f2a3b4` mosquito_events index) |
| Frontend types | `npx tsc --noEmit` | clean |
| Frontend lint | `npm run lint` | 18 errors / 8 warnings, **all pre-existing in files this work never touched** (baseline before this work: 19/8 — one stale warning was removed by the Navbar edit); zero findings in any notification file |

Test suite composition (309 tests): service unit (54), event rules (72), API
(49+18), cross-cluster/cross-user permission isolation (15), MQTT triggers
(22), background jobs + scheduler (37), push providers/channels (25),
concurrency (4), edge cases (13).

## Security review outcome

Full report in the review pass; summary: fan-out-on-write makes read-side
leakage impossible by construction (every read is `WHERE user_id = me`);
identity comes only from the auth dependency; ownership violations 404.
Two HIGH issues were found and **fixed**:

1. Cross-cluster leak via the deprecated `cluster_admins` M2M in
   `notify_cluster` (and its echo in the summaries job) — recipients are now
   exactly cluster members + super admins.
2. `emit()` could leave the caller's session in pending-rollback state after
   an IntegrityError (would 500 API routes / silently break later MQTT emits)
   — now detected and rolled back, with the preference-row race removed at the
   source; verified under a forced FK violation.

Also fixed: ILIKE wildcard escaping, push-subscription metadata carryover on
endpoint re-registration, bounded dispatch pool (ThreadPoolExecutor(4) instead
of thread-per-notification), bulk fan-out reduced to constant query count
(no per-user SELECT/INSERT, no post-commit refresh N+1), `/devices/None`
action_url, FE/service-worker open-redirect guard (`safeUrl`),
`mosquito_events (device_id, timestamp)` index for the surge counter.

## Known limitations / accepted findings

- **Dedupe race**: SELECT-then-INSERT window check; two truly concurrent
  senders with the same dedupe key can both insert (test documents it).
  Strict once-only needs a DB partial-unique constraint — acceptable for the
  current single-process deployment.
- **Push endpoint re-registration** reassigns the row to the authenticated
  caller by design (single-owner browser endpoints; forged endpoints fail
  webpush decryption and deliver nothing). A caller who somehow learns another
  user's endpoint URL could silently detach it (DoS-only, no data exposure).
- **Scheduler is single-instance**: running multiple app replicas would run
  jobs in each replica (duplicate summaries). Gate with `NOTIFY_JOBS_ENABLED=0`
  on extra replicas.
- `USER_APPROVED` / `USER_REJECTED` / `ROLE_CHANGED` events are implemented
  and tested but **unwired** — the app has no user-approval or role-change
  endpoint yet; wire them when such routes exist.
- Deprecated-but-house-convention `datetime.utcnow()` naive-UTC timestamps are
  used throughout to match the existing codebase.

## Activation checklist (deploy)

1. `alembic upgrade head` (two new revisions; idempotent vs dev `create_all`).
2. Optional push: set `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`,
   `VAPID_CLAIMS_EMAIL` in the backend `.env` (see DEPLOYMENT.md for
   generation). Without them everything works and push is silently disabled.
3. All `NOTIFY_*` tunables are optional with defaults (DEPLOYMENT.md table).
4. FE needs no new env vars; `public/sw.js` ships automatically.
