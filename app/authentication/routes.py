from fastapi import APIRouter,Depends,BackgroundTasks
from fastapi.security import HTTPBearer
from app.authentication.models import User
from app.authentication.schema import UserCreate, UserLogin, UserLogout, UserResponse, UserUpdate,UserLoginResponse
from app.core.database import get_db
from sqlalchemy.orm import Session
from app.service.email_service import send_welcome_email
from utils.protected_route import get_current_user

from app.service.user_service import UserService
from fastapi import status



router = APIRouter(
    tags=["authentication"],
)


@router.post("/login",status_code=status.HTTP_200_OK,response_model=UserLoginResponse)
def login(login_details: UserLogin,session: Session=Depends(get_db)):
    try:
        user_service = UserService(session)
        print(f"Login details: {login_details}")
        return user_service.login_user(login_details)
    except Exception as e:
        raise e


@router.post("/register",status_code=status.HTTP_201_CREATED,response_model=UserResponse)
def register(register_details: UserCreate,background_tasks:BackgroundTasks, session: Session=Depends(get_db)):
    try:
        user_service = UserService(session)
        user =user_service.create_user(register_details)
        background_tasks.add_task(send_welcome_email,user.email,user.first_name)
        return  user
    except Exception as e:
        raise e

security = HTTPBearer()
@router.get("/me",dependencies=[Depends(security)])
def me(user: UserResponse = Depends(get_current_user)):
    return user


@router.post("/refresh-token",status_code=status.HTTP_200_OK,response_model=UserLoginResponse)
def refresh_token(refresh_token: str,session: Session=Depends(get_db)):
    try:
        user_service = UserService(session)
        return user_service.refresh_token(refresh_token)
    except Exception as e:
        raise e
    

@router.get("/users",status_code=status.HTTP_200_OK,response_model=list[UserResponse])
def get_users(session: Session = Depends(get_db)):
    try:
        user_service = UserService(session)
        return user_service.get_users()
    except Exception as e:
        raise e
    


@router.get("/users/{user_id}",status_code=status.HTTP_200_OK,response_model=UserResponse)
def get_user_by_id(user_id: int, session: Session = Depends(get_db)):
    try:
        user_service = UserService(session)
        return user_service.get_user_by_id(user_id)
    except Exception as e:
        raise e