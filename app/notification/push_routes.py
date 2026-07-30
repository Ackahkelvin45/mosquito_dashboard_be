from typing import List

from fastapi import APIRouter, Depends, status
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session

from app.authentication.schema import UserResponse
from app.core.database import get_db
from app.notification.schema import (
    PushSubscriptionCreate,
    PushSubscriptionResponse,
    PushUnsubscribe,
    VapidPublicKeyResponse,
)
from app.notification.service import NotificationService
from utils.protected_route import get_current_user

security = HTTPBearer()

router = APIRouter(tags=["push"])


@router.get("/public-key", status_code=status.HTTP_200_OK, response_model=VapidPublicKeyResponse)
def get_public_key(session: Session = Depends(get_db),
                   current_user: UserResponse = Depends(get_current_user)):
    # publicKey is null when push is disabled server-side (VAPID unset).
    try:
        return NotificationService(session).get_vapid_public_key()
    except Exception as e:
        raise e


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED, response_model=PushSubscriptionResponse)
def create_subscription(subscription_data: PushSubscriptionCreate,
                        session: Session = Depends(get_db),
                        current_user: UserResponse = Depends(get_current_user)):
    # Upsert on endpoint: re-subscribing from the same browser refreshes keys,
    # reactivates the subscription and resets its failure count.
    try:
        return NotificationService(session).subscribe_push(current_user.id, subscription_data)
    except Exception as e:
        raise e


@router.get("/subscriptions", status_code=status.HTTP_200_OK, response_model=List[PushSubscriptionResponse])
def list_subscriptions(session: Session = Depends(get_db),
                       current_user: UserResponse = Depends(get_current_user)):
    try:
        return NotificationService(session).list_push_subscriptions(current_user.id)
    except Exception as e:
        raise e


@router.delete("/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
def delete_subscription(unsubscribe_data: PushUnsubscribe,
                        session: Session = Depends(get_db),
                        current_user: UserResponse = Depends(get_current_user)):
    # Idempotent: removing an endpoint that is already gone is still a 204.
    try:
        NotificationService(session).unsubscribe_push(current_user.id, unsubscribe_data.endpoint)
    except Exception as e:
        raise e
