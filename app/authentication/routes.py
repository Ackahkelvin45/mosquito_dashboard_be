from typing import Optional, Union

from fastapi import APIRouter,Depends,BackgroundTasks,Query,HTTPException,Request
from fastapi.security import HTTPBearer
from app.api_access.auth import check_rate_limit
from app.audit.models import AuditAction
from app.audit.recorder import audit
from app.authentication.models import User
from app.core.pagination import Page
from app.authentication.schema import UserCreate, UserLogin, UserLogout, UserResponse, UserUpdate,UserLoginResponse,ResearcherRequestCreate,ResearcherRequestResponse,UpdateResearcherRequest,ForgotPasswordRequest,VerifyOTPRequest,ResetPasswordRequest,MessageResponse,TwoFactorChallengeResponse,TwoFactorVerifyRequest,TwoFactorResendRequest,TwoFactorToggleRequest,RefreshRequest
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


def _client_ip(request: Request) -> str:
    # request.client.host: behind the deploy proxy this is the proxy address
    # until proxy-headers are configured, but it is never attacker-controlled
    # (unlike X-Forwarded-For without a trusted-proxy list).
    return request.client.host if request.client else "unknown"


@router.post("/login",status_code=status.HTTP_200_OK,
             response_model=Union[TwoFactorChallengeResponse, UserLoginResponse])
def login(login_details: UserLogin, request: Request, session: Session=Depends(get_db)):
    # IP-keyed throttle: every attempt costs a bcrypt verify, and 2FA makes
    # this endpoint the natural brute-force target.
    check_rate_limit(_client_ip(request), "auth_login", 10)
    try:
        user_service = UserService(session)
        return user_service.login_user(login_details, ip=_client_ip(request))
    except Exception as e:
        raise e


@router.post("/login/verify-2fa",status_code=status.HTTP_200_OK,response_model=UserLoginResponse)
def verify_two_factor(verify_details: TwoFactorVerifyRequest, request: Request,
                      session: Session=Depends(get_db)):
    check_rate_limit(_client_ip(request), "auth_2fa_verify", 10)
    try:
        return UserService(session).verify_two_factor(
            verify_details.two_factor_token, verify_details.code, ip=_client_ip(request))
    except Exception as e:
        raise e


@router.post("/login/resend-2fa",status_code=status.HTTP_200_OK,response_model=MessageResponse)
def resend_two_factor(resend_details: TwoFactorResendRequest, request: Request,
                      session: Session=Depends(get_db)):
    check_rate_limit(_client_ip(request), "auth_2fa_resend", 5)
    try:
        result = UserService(session).resend_two_factor(
            resend_details.two_factor_token, ip=_client_ip(request))
        return MessageResponse(message=result["message"])
    except Exception as e:
        raise e


@router.post("/me/two-factor",status_code=status.HTTP_200_OK)
def set_two_factor(toggle: TwoFactorToggleRequest, request: Request,
                   session: Session = Depends(get_db),
                   current_user: UserResponse = Depends(get_current_user)):
    """Self-service 2FA toggle. Deliberately NOT part of PATCH /users/{id}:
    nobody may flip someone else's second factor. Enabling invalidates the
    caller's sessions (reauth_required=true) so the next login exercises 2FA."""
    try:
        return UserService(session).set_two_factor(
            current_user.id, toggle.enabled, toggle.current_password,
            ip=_client_ip(request))
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
        user = user_service.create_user(register_details, actor=current_user)
        background_tasks.add_task(send_welcome_email, user.email, user.first_name)
        return user
    except Exception as e:
        raise e

security = HTTPBearer()
@router.get("/me",dependencies=[Depends(security)])
def me(user: UserResponse = Depends(get_current_user)):
    return user


@router.post("/refresh-token",status_code=status.HTTP_200_OK,response_model=UserLoginResponse)
def refresh_token(session: Session=Depends(get_db),
                  refresh_token: Optional[str] = None,
                  body: Optional[RefreshRequest] = None):
    # Body is the right place for a credential (query strings land in server/
    # proxy logs); the query param stays only for already-deployed clients.
    token = body.refresh_token if body else refresh_token
    if not token:
        raise HTTPException(status_code=422, detail="refresh_token is required")
    try:
        user_service = UserService(session)
        return user_service.refresh_token(token)
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
    


@router.patch("/users/{user_id}",status_code=status.HTTP_200_OK,response_model=UserResponse)
def update_user(user_id: int, update_data: UserUpdate, session: Session = Depends(get_db),
                current_user: UserResponse = Depends(require_admin)):
    try:
        service = UserService(session)
        existing = service.get_user_by_id(user_id)
        if not is_super_admin(current_user):
            # Cluster admins manage only their own cluster's users…
            if existing.cluster_id != current_user.cluster_id:
                raise HTTPException(status_code=404, detail="User not found")
            # …may not promote anyone to super admin…
            if update_data.role == UserRole.SUPER_ADMIN:
                raise HTTPException(status_code=403, detail="Only a super admin can grant the super admin role")
            # …and may not move users to another cluster.
            if "cluster_id" in update_data.model_fields_set and update_data.cluster_id != current_user.cluster_id:
                raise HTTPException(status_code=403, detail="Only a super admin can move users between clusters")
        return service.update_user(user_id, update_data, actor=current_user)
    except Exception as e:
        raise e



@router.get("/researcher-requests",status_code=status.HTTP_200_OK,response_model=Page[ResearcherRequestResponse],
            dependencies=[Depends(require_super_admin)])
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
    


@router.patch("/researcher-requests/{request_id}/status",status_code=status.HTTP_200_OK,response_model=ResearcherRequestResponse)
def update_researcher_request_status(request_id: int, status: str, background_tasks:BackgroundTasks,session: Session = Depends(get_db),
                                     current_user: UserResponse = Depends(require_super_admin)):
    # SUPER_ADMIN only: approval grants cluster access. Previously any
    # authenticated user could approve requests — including their own.
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
        audit(session, AuditAction.RESEARCHER_REQUEST_DECIDED,
              actor_user_id=current_user.id, actor_email=current_user.email,
              target_type="researcher_request", target_id=request_id,
              detail={"status": normalized_status})
        return researcher_request
    except Exception as e:
        raise e



@router.patch("/researcher-requests/{request_id}",status_code=status.HTTP_200_OK,response_model=ResearcherRequestResponse,
              dependencies=[Depends(require_super_admin)])
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
