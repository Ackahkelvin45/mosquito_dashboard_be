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
    if not authorization:
        raise UnauthorizedException()
    if not authorization.startswith(AUTH_HEADER_TYPE):
        raise UnauthorizedException()
    
    access_token=authorization.split(" ")[1]

    payload=AuthHandler.decode_token(access_token)
    if payload and payload.get("sub"):
        user_id=payload.get("sub")
        user=UserService(session).get_user_by_id(user_id)
        if not user:
            raise UnauthorizedException()
        return UserResponse.model_validate(user)
    else:
        raise UnauthorizedException()
    

