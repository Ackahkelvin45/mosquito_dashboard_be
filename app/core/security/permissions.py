"""Role checks and cluster-scoping helpers.

Two separate concerns:
  * ROLE gating — who may call an endpoint at all (require_* dependencies).
  * DATA scoping — which clusters' data a caller may see (visible_cluster_ids).

Scoping is enforced server-side regardless of what the client sends: a scoped
caller asking for a device outside their scope gets a 404 (we don't reveal that
it exists), not a filtered-but-present result.
"""
from typing import Optional

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.authentication.enums import UserRole
from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.device.models import DeviceCluster
from utils.protected_route import get_current_user


def require_roles(*allowed: UserRole):
    """Dependency factory — 403 unless the caller has one of `allowed` roles."""
    allowed_set = set(allowed)

    def _dep(user: UserResponse = Depends(get_current_user)) -> UserResponse:
        if user.role not in allowed_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action",
            )
        return user

    return _dep


# Only a super admin.
require_super_admin = require_roles(UserRole.SUPER_ADMIN)
# A cluster admin or a super admin.
require_admin = require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN)


def is_super_admin(user: UserResponse) -> bool:
    return user.role == UserRole.SUPER_ADMIN


def is_guest(user: UserResponse) -> bool:
    return user.role == UserRole.GUEST


def visible_cluster_ids(session: Session, user: UserResponse) -> Optional[set[int]]:
    """Cluster ids the user may see.

    Returns None for a super admin — meaning "no restriction, all clusters".
    Everyone else sees their own cluster plus every public cluster. The
    anonymous GUEST principal has no cluster_id, so it falls through to
    public clusters only.
    """
    if is_super_admin(user):
        return None

    ids: set[int] = set()
    if user.cluster_id is not None:
        ids.add(user.cluster_id)
    public_rows = (
        session.query(DeviceCluster.id).filter(DeviceCluster.public.is_(True)).all()
    )
    ids.update(pid for (pid,) in public_rows)
    return ids


def effective_cluster_scope(
    session: Session,
    user: UserResponse,
    requested_cluster_id: Optional[int] = None,
) -> tuple[Optional[set[int]], bool]:
    """Combine the caller's visible clusters with an optional requested filter.

    Returns (allowed_ids, out_of_scope):
      * allowed_ids None  → unrestricted (super admin, no cluster filter asked).
      * allowed_ids set   → restrict device queries to these cluster ids.
      * out_of_scope True → the caller explicitly asked for a cluster they can't
        see; the caller should return an empty result rather than leak.
    """
    visible = visible_cluster_ids(session, user)

    if visible is None:
        # Super admin: honour whatever filter they asked for, unrestricted otherwise.
        if requested_cluster_id is not None:
            return {requested_cluster_id}, False
        return None, False

    if requested_cluster_id is not None:
        if requested_cluster_id not in visible:
            return set(), True  # asked for a cluster outside scope → empty
        return {requested_cluster_id}, False

    return visible, False


def assert_device_visible(session: Session, user: UserResponse, device) -> None:
    """404 if `device` is outside the caller's cluster scope."""
    visible = visible_cluster_ids(session, user)
    if visible is None:
        return
    if device is None or device.cluster_id not in visible:
        raise HTTPException(status_code=404, detail="Device not found")
