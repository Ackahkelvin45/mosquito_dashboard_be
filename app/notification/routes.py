from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session

from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.core.pagination import Page
from app.core.security.permissions import require_super_admin
from app.notification.enums import (
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
)
from app.notification.schema import (
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
    NotificationResponse,
    ReadAllResponse,
    UnreadCountResponse,
)
from app.notification.service import NotificationService
from utils.protected_route import get_current_user

security = HTTPBearer()

router = APIRouter(tags=["notifications"])


# ── Static routes (MUST come before /{notification_id}) ──────────────────────

@router.get("", status_code=status.HTTP_200_OK, response_model=Page[NotificationResponse])
def list_notifications(
    session: Session = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    unread_only: bool = Query(default=False, description="Only unread notifications"),
    category: Optional[NotificationCategory] = Query(default=None),
    severity: Optional[NotificationSeverity] = Query(default=None),
    type_: Optional[NotificationType] = Query(default=None, alias="type"),
    archived: bool = Query(default=False, description="Show archived instead of active notifications"),
    search: Optional[str] = Query(default=None, description="Search across title and body"),
    sort: str = Query(default="newest", pattern="^(newest|oldest)$"),
    current_user: UserResponse = Depends(get_current_user),
):
    try:
        return NotificationService(session).list_notifications(
            current_user.id,
            page=page, page_size=page_size,
            unread_only=unread_only, category=category,
            severity=severity, notification_type=type_,
            archived=archived, search=search, sort=sort,
        )
    except Exception as e:
        raise e


@router.get("/unread-count", status_code=status.HTTP_200_OK, response_model=UnreadCountResponse)
def unread_count(session: Session = Depends(get_db),
                 current_user: UserResponse = Depends(get_current_user)):
    try:
        return NotificationService(session).unread_count(current_user.id)
    except Exception as e:
        raise e


@router.patch("/read-all", status_code=status.HTTP_200_OK, response_model=ReadAllResponse)
def mark_all_read(session: Session = Depends(get_db),
                  current_user: UserResponse = Depends(get_current_user)):
    try:
        return NotificationService(session).mark_all_read(current_user.id)
    except Exception as e:
        raise e


@router.get("/preferences", status_code=status.HTTP_200_OK, response_model=NotificationPreferenceResponse)
def get_preferences(session: Session = Depends(get_db),
                    current_user: UserResponse = Depends(get_current_user)):
    try:
        return NotificationService(session).get_preferences(current_user.id)
    except Exception as e:
        raise e


@router.put("/preferences", status_code=status.HTTP_200_OK, response_model=NotificationPreferenceResponse)
def update_preferences(preference_data: NotificationPreferenceUpdate,
                       session: Session = Depends(get_db),
                       current_user: UserResponse = Depends(get_current_user)):
    try:
        return NotificationService(session).update_preferences(current_user.id, preference_data)
    except Exception as e:
        raise e


@router.post("/test", status_code=status.HTTP_201_CREATED, response_model=NotificationResponse)
def send_test_notification(session: Session = Depends(get_db),
                           current_user: UserResponse = Depends(require_super_admin)):
    # Super-admin only: creates a TEST notification for the caller.
    try:
        return NotificationService(session).send_test(current_user.id)
    except Exception as e:
        raise e


# ── Per-notification routes ──────────────────────────────────────────────────

@router.patch("/{notification_id}/read", status_code=status.HTTP_200_OK, response_model=NotificationResponse)
def mark_read(notification_id: int, session: Session = Depends(get_db),
              current_user: UserResponse = Depends(get_current_user)):
    try:
        return NotificationService(session).mark_read(notification_id, current_user.id)
    except Exception as e:
        raise e


@router.patch("/{notification_id}/unread", status_code=status.HTTP_200_OK, response_model=NotificationResponse)
def mark_unread(notification_id: int, session: Session = Depends(get_db),
                current_user: UserResponse = Depends(get_current_user)):
    try:
        return NotificationService(session).mark_unread(notification_id, current_user.id)
    except Exception as e:
        raise e


@router.patch("/{notification_id}/archive", status_code=status.HTTP_200_OK, response_model=NotificationResponse)
def archive_notification(notification_id: int, session: Session = Depends(get_db),
                         current_user: UserResponse = Depends(get_current_user)):
    try:
        return NotificationService(session).archive(notification_id, current_user.id)
    except Exception as e:
        raise e


@router.patch("/{notification_id}/unarchive", status_code=status.HTTP_200_OK, response_model=NotificationResponse)
def unarchive_notification(notification_id: int, session: Session = Depends(get_db),
                           current_user: UserResponse = Depends(get_current_user)):
    try:
        return NotificationService(session).unarchive(notification_id, current_user.id)
    except Exception as e:
        raise e


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification(notification_id: int, session: Session = Depends(get_db),
                        current_user: UserResponse = Depends(get_current_user)):
    try:
        NotificationService(session).delete(notification_id, current_user.id)
    except Exception as e:
        raise e
