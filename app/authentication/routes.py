from fastapi import APIRouter,Depends,BackgroundTasks,Query,HTTPException
from fastapi.security import HTTPBearer
from app.authentication.models import User
from app.core.pagination import Page
from app.authentication.schema import UserCreate, UserLogin, UserLogout, UserResponse, UserUpdate,UserLoginResponse,ResearcherRequestCreate,ResearcherRequestResponse,UpdateResearcherRequest,ForgotPasswordRequest,VerifyOTPRequest,ResetPasswordRequest,MessageResponse
from app.core.database import get_db
from sqlalchemy.orm import Session
from app.service.email_service import send_welcome_email,send_researcher_request_email,send_researcher_approved_email,send_researcher_declined_email,send_password_reset_otp_email
from utils.protected_route import get_current_user
from app.core.security.permissions import require_admin, require_super_admin, is_super_admin

from app.notification.events import NotificationEvent, emit
from app.service.user_service import UserService
from app.service.reseacher_request_service import ResearcherRequestService
from fastapi import status
from app.device.models import DeviceCluster
from app.authentication.enums import UserRole, ApprovalStatus



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
def register(register_details: UserCreate, background_tasks:BackgroundTasks,
             session: Session=Depends(get_db),
             current_user: UserResponse = Depends(require_admin)):
    # User creation is an admin action (public self-signup is removed). A super
    # admin may set any role and cluster; a cluster admin may only create plain
    # USERS, and they are forced into the admin's own cluster.
    try:
        if not is_super_admin(current_user):
            register_details.role = UserRole.USER
            register_details.cluster_id = current_user.cluster_id
        # Admin-created accounts are vouched for, so they skip the pending gate.
        register_details.approval_status = ApprovalStatus.APPROVED
        user_service = UserService(session)
        user = user_service.create_user(register_details)
        background_tasks.add_task(send_welcome_email, user.email, user.first_name)
        return user
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
    

@router.post("/forgot-password",status_code=status.HTTP_200_OK,response_model=MessageResponse)
def forgot_password(forgot_details: ForgotPasswordRequest,background_tasks:BackgroundTasks,session: Session=Depends(get_db)):
    try:
        user_service = UserService(session)
        result = user_service.forgot_password(forgot_details.email)
        if result:
            background_tasks.add_task(send_password_reset_otp_email, result["email"], result["first_name"], result["otp"])
        return MessageResponse(message="If an account with that email exists, a verification code has been sent.")
    except Exception as e:
        raise e


@router.post("/verify-otp",status_code=status.HTTP_200_OK,response_model=MessageResponse)
def verify_otp(verify_details: VerifyOTPRequest,session: Session=Depends(get_db)):
    try:
        user_service = UserService(session)
        user_service.verify_otp(verify_details.email, verify_details.otp)
        return MessageResponse(message="OTP verified successfully.")
    except Exception as e:
        raise e


@router.post("/reset-password",status_code=status.HTTP_200_OK,response_model=MessageResponse)
def reset_password(reset_details: ResetPasswordRequest,session: Session=Depends(get_db)):
    try:
        user_service = UserService(session)
        user_service.reset_password(reset_details.email, reset_details.otp, reset_details.new_password)
        return MessageResponse(message="Password reset successfully.")
    except Exception as e:
        raise e


@router.get("/users",status_code=status.HTTP_200_OK,response_model=Page[UserResponse])
def get_users(session: Session = Depends(get_db),
              page: int = Query(default=1, ge=1),
              page_size: int = Query(default=20, ge=1, le=100),
              email: str = None,
              name: str = None,
              role: str = None,
              approval_status: str = None,
              current_user: UserResponse = Depends(require_admin),
              ):
    # Super admin sees everyone; a cluster admin sees only their cluster's users.
    try:
        restrict_cluster_id = None if is_super_admin(current_user) else current_user.cluster_id
        user_service = UserService(session)
        return user_service.get_users(
            page=page,
            page_size=page_size,
            email=email,
            name=name,
            role=role,
            approval_status=approval_status,
            restrict_cluster_id=restrict_cluster_id,
        )
    except Exception as e:
        raise e



@router.get("/users/{user_id}",status_code=status.HTTP_200_OK,response_model=UserResponse)
def get_user_by_id(user_id: int, session: Session = Depends(get_db),
                   current_user: UserResponse = Depends(require_admin)):
    try:
        user = UserService(session).get_user_by_id(user_id)
        # A cluster admin may only view users within their own cluster.
        if not is_super_admin(current_user) and user.cluster_id != current_user.cluster_id:
            raise HTTPException(status_code=404, detail="User not found")
        return user
    except Exception as e:
        raise e
    


@router.get("/researcher-requests",status_code=status.HTTP_200_OK,response_model=Page[ResearcherRequestResponse],
            dependencies=[Depends(get_current_user)])
def get_researcher_requests(session: Session = Depends(get_db),
                            page: int = Query(default=1, ge=1),
                            page_size: int = Query(default=20, ge=1, le=100)):
    try:
        researcher_request_service = ResearcherRequestService(session)
        return researcher_request_service.get_reseachers_requests(page=page, page_size=page_size)
    except Exception as e:
        raise e


# Deliberately unauthenticated: signup POSTs here immediately after registering,
# before the user has ever logged in and therefore before any token exists.
@router.post("/researcher-requests",status_code=status.HTTP_201_CREATED,response_model=ResearcherRequestResponse)
def create_researcher_request(request_data: ResearcherRequestCreate, background_tasks:BackgroundTasks,session: Session = Depends(get_db)):
    try:
        researcher_request_service = ResearcherRequestService(session)
        researcher_request = researcher_request_service.create_researcher_request(request_data)
        background_tasks.add_task(send_researcher_request_email, researcher_request.user.email, researcher_request.user.first_name)
        return researcher_request
    except Exception as e:
        raise e
    


@router.patch("/researcher-requests/{request_id}/status",status_code=status.HTTP_200_OK,response_model=ResearcherRequestResponse,
              dependencies=[Depends(get_current_user)])
def update_researcher_request_status(request_id: int, status: str, background_tasks:BackgroundTasks,session: Session = Depends(get_db)):
    try:
        normalized_status = status.strip().lower()
        if normalized_status == "declined":
            normalized_status = "rejected"

        researcher_request_service = ResearcherRequestService(session)
        researcher_request=researcher_request_service.update_researcher_request_status(request_id, normalized_status)
        if normalized_status == "approved":
            cluster_uuid = None
            cluster = None
            if researcher_request.cluster_id is not None:
                cluster = session.query(DeviceCluster).filter(DeviceCluster.id == researcher_request.cluster_id).first()
                if cluster:
                    cluster_uuid = cluster.cluster_uuid
            background_tasks.add_task(
                send_researcher_approved_email,
                researcher_request.user.email,
                researcher_request.user.first_name,
                cluster_uuid,
            )
            emit(session, NotificationEvent.RESEARCHER_REQUEST_APPROVED,
                 user=researcher_request.user, cluster=cluster)
        elif normalized_status == "rejected":
            background_tasks.add_task(send_researcher_declined_email, researcher_request.user.email, researcher_request.user.first_name)
            emit(session, NotificationEvent.RESEARCHER_REQUEST_REJECTED, user=researcher_request.user)
        return researcher_request
    except Exception as e:
        raise e



@router.patch("/researcher-requests/{request_id}",status_code=status.HTTP_200_OK,response_model=ResearcherRequestResponse,
              dependencies=[Depends(get_current_user)])
def update_researcher_request(request_id: int, request_data: UpdateResearcherRequest, background_tasks:BackgroundTasks,session: Session = Depends(get_db)):
    try:
        researcher_request_service = ResearcherRequestService(session)
        researcher_request=researcher_request_service.update_reseacher_request(request_id, request_data)
        normalized_status = (request_data.status or "").strip().lower()
        if normalized_status == "declined":
            normalized_status = "rejected"

        if normalized_status == "approved":
            cluster_uuid = None
            cluster = None
            if researcher_request.cluster_id is not None:
                cluster = session.query(DeviceCluster).filter(DeviceCluster.id == researcher_request.cluster_id).first()
                if cluster:
                    cluster_uuid = cluster.cluster_uuid
            background_tasks.add_task(
                send_researcher_approved_email,
                researcher_request.user.email,
                researcher_request.user.first_name,
                cluster_uuid,
            )
            emit(session, NotificationEvent.RESEARCHER_REQUEST_APPROVED,
                 user=researcher_request.user, cluster=cluster)
        elif normalized_status == "rejected":
            background_tasks.add_task(send_researcher_declined_email, researcher_request.user.email, researcher_request.user.first_name)
            emit(session, NotificationEvent.RESEARCHER_REQUEST_REJECTED, user=researcher_request.user)
        return researcher_request
    except Exception as e:
        raise e
