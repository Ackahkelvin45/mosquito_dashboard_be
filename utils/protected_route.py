from datetime import datetime, timezone

from fastapi import HTTPException,Depends,status,Header
from sqlalchemy.orm import Session
from typing import Annotated,Union
from app.authentication.models import User
from app.authentication.enums import ApprovalStatus, UserRole
from app.core.security.authhandler import AuthHandler
from app.service.user_service import UserService
from app.core.database import get_db
from app.authentication.schema import UserResponse
from app.exceptions.exceptions import UnauthorizedException


AUTH_HEADER_TYPE = "Bearer"
AUTH_HEADER_NAME = "Authorization"

# Sentinel id for the synthetic guest principal — no user row ever has id 0.
GUEST_USER_ID = 0


def guest_user() -> UserResponse:
    """The anonymous read-only principal used by get_current_user_or_guest.

    Built fresh per request so no handler can mutate shared state. Role GUEST
    with no cluster_id means visible_cluster_ids() resolves to public clusters
    only, and every require_roles() gate rejects it.
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return UserResponse(
        id=GUEST_USER_ID,
        first_name="Guest",
        last_name="Viewer",
        email="guest@example.com",
        is_active=True,
        role=UserRole.GUEST,
        approval_status=ApprovalStatus.APPROVED,
        cluster_id=None,
        cluster=None,
        created_at=epoch,
        updated_at=epoch,
    )

async def get_current_user(
    session: Session = Depends(get_db),
    authorization:Annotated[Union[str,None],Header(alias=AUTH_HEADER_NAME)] = None
) -> UserResponse:
    """Resolve the authenticated user, or raise 401.

    Use this as the dependency on protected routes — NOT bare `HTTPBearer`,
    which only asserts that a header is present and never inspects it.
    """
    if not authorization:
        raise UnauthorizedException()

    # Split on the first space: `startswith` alone would accept "Bearerxyz",
    # and an index-based split blows up on a header with no space at all.
    scheme, _, raw_token = authorization.partition(" ")
    if scheme != AUTH_HEADER_TYPE:
        raise UnauthorizedException()

    access_token = raw_token.strip()
    if not access_token:
        raise UnauthorizedException()

    # verify_token enforces `type == "access"`. decode_token does not, which
    # let a refresh token be replayed as an access token.
    user_id = AuthHandler.verify_token(access_token, expected_type="access")

    try:
        user = UserService(session).get_user_by_id(user_id)
    except HTTPException:
        # Token was valid but the user is gone — that's an auth failure, not a 404.
        raise UnauthorizedException()

    if not user:
        raise UnauthorizedException()
    return user


async def get_current_user_or_guest(
    session: Session = Depends(get_db),
    authorization:Annotated[Union[str,None],Header(alias=AUTH_HEADER_NAME)] = None
) -> UserResponse:
    """Resolve the caller for PUBLIC READ endpoints only.

    No Authorization header → the synthetic GUEST principal (public clusters
    only). A header that IS present must still be a valid access token — an
    expired or malformed token is a 401, never silently downgraded to guest,
    so the frontend's token-refresh flow keeps working.

    Never use this on a mutation endpoint: writes stay on get_current_user /
    require_roles, which reject anonymous callers outright.
    """
    if not authorization:
        return guest_user()
    return await get_current_user(session=session, authorization=authorization)


