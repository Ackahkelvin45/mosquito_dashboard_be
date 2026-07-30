"""Shared test infrastructure for the notification system suite.

- SQLite in-memory engine (StaticPool) with the full Base.metadata schema.
- Per-test isolation via drop_all/create_all teardown.
- App fixture builds a fresh FastAPI() with ONLY the notification + push
  routers (never the real app lifespan — no MQTT, no real DB).
- Channel dispatch (daemon thread + fresh SessionLocal in production) is
  replaced by a synchronous recorder so tests are deterministic.
"""
import itertools
import os
import uuid
from datetime import datetime, timedelta

# Force a harmless DATABASE_URL BEFORE any app import: app.core.database builds
# its engine at import time; pointing it at in-memory sqlite guarantees no test
# can ever touch the real database, even through an unpatched SessionLocal.
os.environ["DATABASE_URL"] = "sqlite://"
# Startup-safe defaults for every required Settings field so tests run even
# without the local .env (CI).
_SETTINGS_DEFAULTS = {
    "JWT_SECRET_KEY": "test-secret",
    "JWT_ALGORITHM": "HS256",
    "JWT_EXPIRATION_TIME": "3600",
    "JWT_REFRESH_TOKEN_EXPIRE_SECONDS": "86400",
    "RESEND_API_KEY": "test-resend-key",
    "EMAIL_FROM": "test@example.com",
    "MQTT_BROKER": "localhost",
    "MQTT_PORT": "1883",
    "TOPIC_SENSOR_DATA": "mosquito_dashboard/+/sensor_data",
    "TOPIC_MOSQUITO_COUNT": "mosquito_dashboard/+/mosquito_data",
    "MQTT_CLIENT_ID": "test-client",
}
for _key, _value in _SETTINGS_DEFAULTS.items():
    os.environ.setdefault(_key, _value)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db

# Import every model module so Base.metadata knows the full schema.
import app.authentication.models  # noqa: F401
import app.device.models  # noqa: F401
import app.notification.models  # noqa: F401

from app.authentication.enums import ApprovalStatus, UserRole
from app.authentication.models import User
from app.authentication.schema import UserResponse
from app.device.models import Device, DeviceCluster
from app.notification.enums import (
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
)
from app.notification.models import Notification
from app.notification.service import NotificationService
from utils.protected_route import get_current_user

import app.notification.service as _service_module

# Captured BEFORE any monkeypatching so the production thread-dispatch contract
# can still be asserted in a dedicated test.
ORIGINAL_DISPATCH_CHANNELS = _service_module._dispatch_channels


# ── Engine / session ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def TestingSessionLocal(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def db_session(engine, TestingSessionLocal):
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


# ── Deterministic channel dispatch ───────────────────────────────────────────

@pytest.fixture(autouse=True)
def dispatch_recorder(monkeypatch):
    """Replace the daemon-thread channel dispatch with an in-test recorder.

    Production `_dispatch_channels` spawns a thread with its own (postgres)
    SessionLocal; in tests it just records the entries it was asked to deliver.
    Tests assert against this list; dedicated channel tests exercise
    send_push/send_email directly.
    """
    calls: list = []

    def _record(entries):
        calls.extend(entries)

    monkeypatch.setattr("app.notification.service._dispatch_channels", _record)
    return calls


# ── App / client / auth ──────────────────────────────────────────────────────

@pytest.fixture
def app(db_session):
    from app.notification.push_routes import router as push_router
    from app.notification.routes import router as notification_router

    application = FastAPI()
    application.include_router(notification_router, prefix="/notifications")
    application.include_router(push_router, prefix="/push")
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def login_as(app):
    """Override get_current_user to return the given seeded user.

    require_super_admin composes via require_roles -> Depends(get_current_user),
    so overriding get_current_user alone covers the role-gated routes too.
    """

    def _login(user: User) -> UserResponse:
        response = UserResponse.model_validate(user)
        app.dependency_overrides[get_current_user] = lambda: response
        return response

    return _login


# ── Seed helpers ─────────────────────────────────────────────────────────────

_counter = itertools.count(1)


@pytest.fixture
def make_user(db_session):
    def _make(
        role: UserRole = UserRole.USER,
        cluster_id: int | None = None,
        *,
        is_active: bool = True,
        email: str | None = None,
        first_name: str = "Test",
        last_name: str = "User",
    ) -> User:
        n = next(_counter)
        user = User(
            email=email or f"user{n}-{uuid.uuid4().hex[:6]}@example.com",
            first_name=first_name,
            last_name=last_name,
            hashed_password="not-a-real-hash",
            is_active=is_active,
            approval_status=ApprovalStatus.APPROVED,
            role=role,
            cluster_id=cluster_id,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user

    return _make


@pytest.fixture
def make_cluster(db_session):
    def _make(name: str | None = None, *, public: bool = False) -> DeviceCluster:
        n = next(_counter)
        cluster = DeviceCluster(
            name=name or f"cluster-{n}-{uuid.uuid4().hex[:6]}",
            description="test cluster",
            public=public,
        )
        db_session.add(cluster)
        db_session.commit()
        db_session.refresh(cluster)
        return cluster

    return _make


@pytest.fixture
def make_device(db_session):
    def _make(
        cluster: DeviceCluster | None = None,
        *,
        name: str | None = None,
        last_activity: datetime | None = None,
        **overrides,
    ) -> Device:
        n = next(_counter)
        device = Device(
            device_uuid=f"dev-{n}-{uuid.uuid4().hex[:6]}",
            name=name or f"Device {n}",
            cluster_id=cluster.id if cluster is not None else None,
            last_activity=last_activity or datetime.utcnow(),
            **overrides,
        )
        db_session.add(device)
        db_session.commit()
        db_session.refresh(device)
        return device

    return _make


@pytest.fixture
def make_notification(db_session):
    def _make(user: User, **overrides) -> Notification:
        fields = dict(
            user_id=user.id,
            title="Test title",
            body="Test body",
            notification_type=NotificationType.TEST,
            severity=NotificationSeverity.INFO,
            category=NotificationCategory.SYSTEM,
            delivered_at=datetime.utcnow(),
        )
        fields.update(overrides)
        notification = Notification(**fields)
        db_session.add(notification)
        db_session.commit()
        db_session.refresh(notification)
        return notification

    return _make


@pytest.fixture
def service(db_session):
    return NotificationService(db_session)


@pytest.fixture
def backdate(db_session):
    """Shift a notification's created_at into the past (dedupe-window tests)."""

    def _backdate(notification: Notification, minutes: int) -> None:
        db_session.query(Notification).filter(
            Notification.id == notification.id
        ).update({"created_at": datetime.utcnow() - timedelta(minutes=minutes)})
        db_session.commit()

    return _backdate


@pytest.fixture
def backdate_dedupe_key(db_session):
    """Backdate every row carrying a dedupe_key (fan-out creates many rows)."""

    def _backdate(dedupe_key: str, minutes: int) -> None:
        db_session.query(Notification).filter(
            Notification.dedupe_key == dedupe_key
        ).update({"created_at": datetime.utcnow() - timedelta(minutes=minutes)})
        db_session.commit()

    return _backdate
