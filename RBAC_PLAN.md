# Role-Based Access + Cluster Scoping

Status: **BUILT & enforced** (2026-07-29). Backend enforcement, cluster scoping,
role-aware frontend, account promotions, and public-signup removal are all live
and verified (36-check `verify_rbac.py`, all green). The spec below is the
implemented behaviour. One item deferred: dropping the `cluster_admins` M2M
table (access control uses `users.cluster_id` + role; the old table is now
unused for auth but not physically dropped — safe cleanup for later).

## Roles

`USER`, `ADMIN` (of one cluster), `SUPER_ADMIN`. A user belongs to exactly one
cluster (`users.cluster_id`, nullable). Super admins belong to no cluster.

## Visibility rule (applies to every read surface)

- **SUPER_ADMIN** → every device, every cluster.
- **ADMIN / USER** → devices in (their own cluster) ∪ (all public clusters).
- **cluster_id = NULL** → public clusters only.

## Permission matrix

| Action | SUPER_ADMIN | ADMIN (own cluster) | USER |
|---|---|---|---|
| Dashboard / map / historical / device list | all clusters | own + public | own + public |
| Download CSV / PDF | ✅ | ✅ | ✅ |
| Create user | any role, any cluster | USER only, **auto-assigned to admin's own cluster** | ❌ |
| List / manage users | all | own cluster only | ❌ |
| Create device | ✅ | ❌ | ❌ |
| Edit device | any | own cluster | ❌ |
| Delete device | ✅ | ❌ | ❌ |
| Create cluster + assign its admin | ✅ | ❌ | ❌ |
| Edit / delete cluster | ✅ | ❌ | ❌ |
| Approve researcher requests | ✅ | ❌ | ❌ |

## UI scoping rules (frontend) — from user, 2026-07-29

The **cluster filter control must be HIDDEN** for anyone who is not a super
admin, because a scoped user only has one cluster to see — a cluster picker is
meaningless and would imply access they don't have.

- **Map** — show only devices in the user's visible clusters. **No cluster
  filter** shown for USER/ADMIN. Super admin keeps the cluster filter.
- **Historical data** — show only rows for the user's visible clusters. **No
  cluster filter** for USER/ADMIN. Super admin keeps it.
- **Dashboard** — same: its `cluster_id` selector is super-admin-only.
- **Devices list** — same: cluster filter super-admin-only; list scoped.

> ⚠️ CONFIRM: the historical-data description was ambiguous ("I should be able
> to filter also by cluster"). Working assumption: a plain USER has ONE cluster,
> so the cluster filter is hidden for them exactly like the map. Only SUPER_ADMIN
> filters by cluster. Confirm before building.

## Enforcement (backend) — the real guard

UI hiding is cosmetic; the API must enforce scope regardless of what the client
sends:

- A `get_scoped_cluster_ids(user)` helper → `None` for super admin (no filter),
  else `{user.cluster_id} ∪ {public cluster ids}`.
- Apply it to: `/dashboard`, `/devices`, `/devices/{id}`, `/devices/{id}/charts`,
  `/mosquito`, sensor-readings, mosquito-events. A scoped user requesting a
  device outside their scope → **404** (not 403 — don't reveal it exists).
- If a scoped user passes `cluster_id`/`region`/`device_id` that resolves
  outside their scope, intersect with their allowed set rather than trusting it.
- Lock create/delete device + cluster to SUPER_ADMIN; edit device to
  SUPER_ADMIN or the device's-cluster ADMIN.
- `/auth/register` → authenticated: SUPER_ADMIN sets any role/cluster; ADMIN
  forced to role=USER + own cluster. (Currently still PUBLIC — open hole.)
- Force role/approval server-side; remove public self-signup.

## Data migration (when we build it)

- Promote to SUPER_ADMIN: `ackahkelvin455` (done 2026-07-29), `ackahkelvin464`,
  `kkrasta021`.
- `ackahk492` → ADMIN, cluster_id = 1.
- Collapse `cluster_admins` M2M → derive admins from `cluster_id` + role, drop
  the table.

## Researcher-request flow

Currently rides on public self-signup. When signup is removed it becomes
orphaned. Decision pending: keep dormant (default) or remove.
