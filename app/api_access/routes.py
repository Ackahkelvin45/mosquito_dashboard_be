"""API key management — JWT-authenticated, used by the dashboard's APIs page."""
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.api_access.models import ApiKey
from app.api_access.schema import ApiKeyCreate, ApiKeyCreatedResponse, ApiKeyResponse
from app.audit.models import AuditAction
from app.audit.recorder import audit
from app.authentication.enums import ApprovalStatus
from app.authentication.models import User
from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.core.security.permissions import is_super_admin, require_super_admin
from utils.protected_route import get_current_user

router = APIRouter(tags=["api-keys"])


def _require_active_approved(user: UserResponse) -> None:
    # Login only checks the password — a PENDING/REJECTED or deactivated user
    # can still hold a valid JWT, so key creation re-checks explicitly.
    if not user.is_active or user.approval_status != ApprovalStatus.APPROVED:
        raise HTTPException(
            status_code=403,
            detail="Only active, approved accounts can create API keys",
        )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiKeyCreatedResponse,
    summary="Create an API key (the raw key is returned only in this response)",
)
def create_api_key(
    payload: ApiKeyCreate,
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    _require_active_approved(current_user)
    raw, prefix, key_hash = ApiKey.generate()
    key = ApiKey(
        name=payload.name,
        description=payload.description,
        key_prefix=prefix,
        key_hash=key_hash,
        user_id=current_user.id,
        expires_at=(
            datetime.utcnow() + timedelta(days=payload.expires_in_days)
            if payload.expires_in_days
            else None
        ),
    )
    session.add(key)
    session.commit()
    session.refresh(key)
    audit(session, AuditAction.API_KEY_CREATED, actor_user_id=current_user.id,
          actor_email=current_user.email, target_type="api_key", target_id=key.id,
          detail={"name": key.name, "prefix": key.key_prefix})
    return ApiKeyCreatedResponse(
        **ApiKeyResponse.model_validate(key).model_dump(),
        api_key=raw,
    )


@router.get(
    "",
    response_model=List[ApiKeyResponse],
    summary="List your API keys (metadata only; ?all=true for super admins)",
)
def list_api_keys(
    all: bool = Query(default=False, description="Super admin only: list every user's keys"),
    search: Optional[str] = Query(default=None, description="With all=true: matches owner email or key name"),
    status_filter: Optional[Literal["active", "revoked", "expired"]] = Query(
        default=None, alias="status", description="With all=true: filter by key status"
    ),
    sort: Literal["created_at", "last_used"] = Query(default="created_at", description="With all=true"),
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    if all:
        if not is_super_admin(current_user):
            raise HTTPException(status_code=403, detail="Only a super admin can list all API keys")
        q = (
            session.query(ApiKey)
            .join(User, ApiKey.user_id == User.id)
            .options(joinedload(ApiKey.user), joinedload(ApiKey.revoked_by_user))
        )
        if search:
            like = f"%{search}%"
            q = q.filter(or_(User.email.ilike(like), ApiKey.name.ilike(like)))
        now = datetime.utcnow()
        if status_filter == "revoked":
            q = q.filter(ApiKey.revoked_at.isnot(None))
        elif status_filter == "expired":
            q = q.filter(
                ApiKey.revoked_at.is_(None),
                ApiKey.expires_at.isnot(None),
                ApiKey.expires_at <= now,
            )
        elif status_filter == "active":
            q = q.filter(
                ApiKey.revoked_at.is_(None),
                or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
            )
        if sort == "last_used":
            # Never-used keys sort last — "stale keys" is the point of this sort.
            q = q.order_by(ApiKey.last_used_at.desc().nulls_last())
        else:
            q = q.order_by(ApiKey.created_at.desc())

        items = []
        for key in q.all():
            item = ApiKeyResponse.model_validate(key)
            item.owner_email = key.user.email if key.user else None
            item.revoked_by_email = key.revoked_by_user.email if key.revoked_by_user else None
            items.append(item)
        return items

    keys = (
        session.query(ApiKey)
        .filter(ApiKey.user_id == current_user.id)
        .order_by(ApiKey.created_at.desc())
        .all()
    )
    return [ApiKeyResponse.model_validate(key) for key in keys]


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key (immediate, irreversible)",
)
def revoke_api_key(
    key_id: int,
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    key = session.get(ApiKey, key_id)
    # 404 for someone else's key — don't reveal that it exists.
    if key is None or (key.user_id != current_user.id and not is_super_admin(current_user)):
        raise HTTPException(status_code=404, detail="API key not found")
    if key.revoked_at is None:
        key.revoked_at = datetime.utcnow()
        key.revoked_by = current_user.id
        session.commit()
        audit(session, AuditAction.API_KEY_REVOKED, actor_user_id=current_user.id,
              actor_email=current_user.email, target_type="api_key", target_id=key.id,
              detail={"prefix": key.key_prefix, "owner_user_id": key.user_id})
    # Revoking an already-revoked key is a no-op 204 (idempotent delete).


@router.post(
    "/revoke-user/{user_id}",
    summary="Super admin: revoke every active key belonging to a user",
)
def revoke_user_keys(
    user_id: int,
    session: Session = Depends(get_db),
    current_user: UserResponse = Depends(require_super_admin),
):
    # Unknown user == user with no active keys: {"revoked": 0}, no existence leak.
    count = (
        session.query(ApiKey)
        .filter(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
        .update(
            {ApiKey.revoked_at: datetime.utcnow(), ApiKey.revoked_by: current_user.id},
            synchronize_session=False,
        )
    )
    session.commit()
    if count:
        audit(session, AuditAction.API_KEYS_BULK_REVOKED, actor_user_id=current_user.id,
              actor_email=current_user.email, target_type="user", target_id=user_id,
              detail={"revoked": count})
    return {"revoked": count}
