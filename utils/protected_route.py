from fastapi import HTTPException,Depends,status,Header
from sqlalchemy.orm import Session
from typing import Annotated,Union
from app.authentication.models import User
from app.core.security.authhandler import AuthHandler
from app.service.user_service import UserService
from app.core.database import get_db
from app.authentication.schema import UserResponse
from app.exceptions.exceptions import UnauthorizedException


AUTH_HEADER_TYPE = "Bearer"
AUTH_HEADER_NAME = "Authorization"

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
    

