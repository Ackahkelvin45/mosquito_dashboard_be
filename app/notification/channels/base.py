"""Delivery channel abstraction.

The in-app channel is the Notification row itself; PUSH and EMAIL are the
pluggable extra transports. Future channels (sms, webhook, ...) implement the
same ABC and slot in without touching call sites.
"""
from abc import ABC, abstractmethod

from sqlalchemy.orm import Session

from app.authentication.models import User
from app.notification.models import Notification


class NotificationChannel(ABC):
    def __init__(self, session: Session):
        self.session = session

    @abstractmethod
    def send(self, notification: Notification, user: User) -> bool:
        """Deliver `notification` to `user`. True on (any) successful delivery.

        Must never raise — a channel failure is reported, not propagated.
        """
        raise NotImplementedError
