"""API-key authentication for the public data API (/api/v1).

Completely separate from the JWT path: keys arrive in `X-API-Key`, never in
`Authorization`, so nothing here can interfere with get_current_user.

The principal resolved from a key is its OWNER, and every route then applies
the existing visible_cluster_ids() scoping — a key can never see more than its
owner sees in the app, and owner demotion/deactivation applies on the very
next request.
"""
import logging
import time
from datetime import datetime
from typing import Annotated, Union

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.api_access.models import ApiKey, KEY_PREFIX
from app.authentication.enums import ApprovalStatus
from app.authentication.schema import UserResponse
from app.core.database import get_db

logger = logging.getLogger("api_access")

DATA_RATE_LIMIT_PER_MIN = 120
EXPORT_RATE_LIMIT_PER_MIN = 5

# ponytail: in-memory fixed-window limiter, per-process — matches the
# single-process uvicorn deployment; move to Redis if workers > 1.
# Keyed on the OWNER's user id, not the key id: otherwise minting more keys
# multiplies the limit.
_windows: dict[tuple[str, int], tuple[int, int]] = {}


def check_rate_limit(user_id: int, bucket: str, limit: int) -> None:
    """Fixed one-minute window per (bucket, owner). 429 above `limit`."""
    now = time.time()
    minute = int(now // 60)
    window_key = (bucket, user_id)
    start, count = _windows.get(window_key, (minute, 0))
    if start != minute:
        start, count = minute, 0
    if count >= limit:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(max(1, 60 - int(now % 60)))},
        )
    _windows[window_key] = (start, count + 1)


def _resolve_key(request: Request, session: Session, raw_key: str | None) -> tuple[UserResponse, ApiKey]:
    if not raw_key or not raw_key.strip().startswith(KEY_PREFIX):
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed API key — pass it in the X-API-Key header",
        )

    key = (
        session.query(ApiKey)
        .filter(ApiKey.key_hash == ApiKey.hash_key(raw_key.strip()))
        .first()
    )
    if key is None or key.is_revoked or key.is_expired:
        # One message for all three: don't tell a caller with a stolen key
        # whether it ever existed.
        raise HTTPException(status_code=401, detail="Invalid, revoked or expired API key")

    owner = key.user
    if (
        owner is None
        or not owner.is_active
        or owner.approval_status != ApprovalStatus.APPROVED
    ):
        raise HTTPException(status_code=401, detail="API key is no longer authorized")

    # Usage bookkeeping: one indexed UPDATE per request, no read-modify-write.
    session.query(ApiKey).filter(ApiKey.id == key.id).update(
        {
            ApiKey.total_requests: ApiKey.total_requests + 1,
            ApiKey.last_used_at: datetime.utcnow(),
        },
        synchronize_session=False,
    )
    session.commit()

    # Audit line: enough to answer "what did this key touch, from where".
    client = request.client.host if request.client else "-"
    logger.info(
        "api_key_request key_id=%s user_id=%s ip=%s method=%s path=%s query=%s",
        key.id, owner.id, client, request.method, request.url.path, request.url.query,
    )
    return UserResponse.model_validate(owner), key


async def get_api_key_principal(
    request: Request,
    session: Session = Depends(get_db),
    x_api_key: Annotated[Union[str, None], Header(alias="X-API-Key")] = None,
) -> tuple[UserResponse, ApiKey]:
    """Auth + rate limit for the query endpoints."""
    principal = _resolve_key(request, session, x_api_key)
    check_rate_limit(principal[0].id, "data", DATA_RATE_LIMIT_PER_MIN)
    return principal


async def get_api_key_principal_export(
    request: Request,
    session: Session = Depends(get_db),
    x_api_key: Annotated[Union[str, None], Header(alias="X-API-Key")] = None,
) -> tuple[UserResponse, ApiKey]:
    """Auth + the much tighter export rate limit."""
    principal = _resolve_key(request, session, x_api_key)
    check_rate_limit(principal[0].id, "export", EXPORT_RATE_LIMIT_PER_MIN)
    return principal
