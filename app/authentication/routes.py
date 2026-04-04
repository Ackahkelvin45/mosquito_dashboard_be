from fastapi import APIRouter,Depends,BackgroundTasks
from fastapi.security import HTTPBearer
from app.authentication.models import User
from app.authentication.schema import UserCreate, UserLogin, UserLogout, UserResponse, UserUpdate,UserLoginResponse,ResearcherRequestCreate,ResearcherRequestResponse,UpdateResearcherRequest
from app.core.database import get_db
from sqlalchemy.orm import Session
from app.service.email_service import send_welcome_email,send_researcher_request_email,send_researcher_approved_email,send_researcher_declined_email
from utils.protected_route import get_current_user

from app.service.user_service import UserService
from app.service.reseacher_request_service import ResearcherRequestService
from fastapi import status
from app.device.models import DeviceCluster



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
def get_users(session: Session = Depends(get_db),
              email: str = None,
              name: str = None,
              role: str = None,
              approval_status: str = None
              ):
    try:
        user_service = UserService(session)
        return user_service.get_users(
            email=email,
            name=name,
            role=role,
            approval_status=approval_status
        )
    except Exception as e:
        raise e
    


@router.get("/users/{user_id}",status_code=status.HTTP_200_OK,response_model=UserResponse)
def get_user_by_id(user_id: int, session: Session = Depends(get_db)):
    try:
        user_service = UserService(session)
        return user_service.get_user_by_id(user_id)
    except Exception as e:
        raise e
    


@router.get("/researcher-requests",status_code=status.HTTP_200_OK,response_model=list[ResearcherRequestResponse])
def get_researcher_requests(session: Session = Depends(get_db)):
    try:
        researcher_request_service = ResearcherRequestService(session)
        return researcher_request_service.get_reseachers_requests()
    except Exception as e:
        raise e


@router.post("/researcher-requests",status_code=status.HTTP_201_CREATED,response_model=ResearcherRequestResponse)
def create_researcher_request(request_data: ResearcherRequestCreate, background_tasks:BackgroundTasks,session: Session = Depends(get_db)):
    try:
        researcher_request_service = ResearcherRequestService(session)
        researcher_request = researcher_request_service.create_researcher_request(request_data)
        background_tasks.add_task(send_researcher_request_email, researcher_request.user.email, researcher_request.user.first_name)
        return researcher_request
    except Exception as e:
        raise e
    


@router.patch("/researcher-requests/{request_id}/status",status_code=status.HTTP_200_OK,response_model=ResearcherRequestResponse)
def update_researcher_request_status(request_id: int, status: str, background_tasks:BackgroundTasks,session: Session = Depends(get_db)):
    try:
        normalized_status = status.strip().lower()
        if normalized_status == "declined":
            normalized_status = "rejected"

        researcher_request_service = ResearcherRequestService(session)
        researcher_request=researcher_request_service.update_researcher_request_status(request_id, normalized_status)
        if normalized_status == "approved":
            cluster_uuid = None
            cluster_password = None
            if researcher_request.cluster_id is not None:
                cluster = session.query(DeviceCluster).filter(DeviceCluster.id == researcher_request.cluster_id).first()
                if cluster:
                    cluster_uuid = cluster.cluster_uuid
                    cluster_password = cluster.password
            background_tasks.add_task(
                send_researcher_approved_email,
                researcher_request.user.email,
                researcher_request.user.first_name,
                cluster_uuid,
                cluster_password,
            )
        elif normalized_status == "rejected":
            background_tasks.add_task(send_researcher_declined_email, researcher_request.user.email, researcher_request.user.first_name)
        return researcher_request
    except Exception as e:
        raise e
    


@router.patch("/researcher-requests/{request_id}",status_code=status.HTTP_200_OK,response_model=ResearcherRequestResponse)
def update_researcher_request(request_id: int, request_data: UpdateResearcherRequest, background_tasks:BackgroundTasks,session: Session = Depends(get_db)):
    try:
        researcher_request_service = ResearcherRequestService(session)
        researcher_request=researcher_request_service.update_reseacher_request(request_id, request_data)
        normalized_status = (request_data.status or "").strip().lower()
        if normalized_status == "declined":
            normalized_status = "rejected"

        if normalized_status == "approved":
            cluster_uuid = None
            cluster_password = None
            if researcher_request.cluster_id is not None:
                cluster = session.query(DeviceCluster).filter(DeviceCluster.id == researcher_request.cluster_id).first()
                if cluster:
                    cluster_uuid = cluster.cluster_uuid
                    cluster_password = cluster.password
            background_tasks.add_task(
                send_researcher_approved_email,
                researcher_request.user.email,
                researcher_request.user.first_name,
                cluster_uuid,
                cluster_password,
            )
        elif normalized_status == "rejected":
            background_tasks.add_task(send_researcher_declined_email, researcher_request.user.email, researcher_request.user.first_name)
        return researcher_request
    except Exception as e:
        raise e
