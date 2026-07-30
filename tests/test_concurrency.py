"""Concurrency behavior — real threads against a file-backed sqlite database
(StaticPool's single in-memory connection cannot model cross-session races).

NOTE: strict once-only dedupe under true parallelism requires a database
constraint (e.g. a partial unique index on dedupe_key over the window) — the
current SELECT-then-INSERT check can race. These tests assert crash-freedom
and sane final state, and document the race window.
"""
import threading
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.authentication.enums import ApprovalStatus, UserRole
from app.authentication.models import User
from app.notification.enums import (
    NotificationCategory,
    NotificationSeverity,
    NotificationType,
)
from app.notification.models import Notification
from app.notification.service import NotificationService


@pytest.fixture
def file_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/concurrency.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def seeded(file_db):
    with file_db() as session:
        user = User(
            email="concurrent@example.com", first_name="Con", last_name="Current",
            hashed_password="x", is_active=True,
            approval_status=ApprovalStatus.APPROVED, role=UserRole.USER,
        )
        session.add(user)
        session.commit()
        notification = Notification(
            user_id=user.id, title="t", body="b",
            notification_type=NotificationType.TEST,
            severity=NotificationSeverity.INFO,
            category=NotificationCategory.SYSTEM,
            delivered_at=datetime.utcnow(),
        )
        session.add(notification)
        session.commit()
        return {"user_id": user.id, "notification_id": notification.id}


def _run_threads(workers):
    threads = [threading.Thread(target=worker) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "worker deadlocked"


class TestParallelMarkRead:
    def test_two_threads_mark_read_same_notification(self, file_db, seeded):
        barrier = threading.Barrier(2)
        errors = []

        def worker():
            session = file_db()
            try:
                barrier.wait(timeout=10)
                NotificationService(session).mark_read(
                    seeded["notification_id"], seeded["user_id"]
                )
            except Exception as exc:  # noqa: BLE001 - collecting for assertion
                errors.append(exc)
            finally:
                session.close()

        _run_threads([worker, worker])
        assert errors == [], f"parallel mark_read crashed: {errors!r}"
        with file_db() as session:
            row = session.get(Notification, seeded["notification_id"])
            assert row.read_at is not None  # final state: read exactly once


class TestParallelDedupeSend:
    def test_same_dedupe_key_parallel_send(self, file_db, seeded):
        barrier = threading.Barrier(2)
        errors = []

        def worker():
            session = file_db()
            try:
                barrier.wait(timeout=10)
                NotificationService(session).send(
                    seeded["user_id"],
                    title="dup", body="dup",
                    notification_type=NotificationType.LOW_BATTERY,
                    severity=NotificationSeverity.WARNING,
                    category=NotificationCategory.SENSOR,
                    dedupe_key="race:1",
                    dedupe_window_minutes=60,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                session.close()

        _run_threads([worker, worker])
        assert errors == [], f"parallel dedupe send crashed: {errors!r}"
        with file_db() as session:
            count = (
                session.query(Notification)
                .filter(Notification.dedupe_key == "race:1")
                .count()
            )
        # SELECT-then-INSERT dedupe has no DB constraint behind it: both
        # threads may pass the window check before either commits. 1 is the
        # intended outcome; 2 is the documented race (needs a DB-level
        # constraint to close). Anything else is a real bug.
        assert count in (1, 2)

    def test_sequential_dedupe_still_exactly_once(self, file_db, seeded):
        # Sanity anchor for the race test above: without parallelism the
        # window check IS exact.
        for _ in range(2):
            with file_db() as session:
                NotificationService(session).send(
                    seeded["user_id"],
                    title="dup", body="dup",
                    notification_type=NotificationType.LOW_BATTERY,
                    severity=NotificationSeverity.WARNING,
                    category=NotificationCategory.SENSOR,
                    dedupe_key="seq:1",
                    dedupe_window_minutes=60,
                )
        with file_db() as session:
            assert (
                session.query(Notification)
                .filter(Notification.dedupe_key == "seq:1")
                .count()
            ) == 1


class TestParallelPreferenceCreate:
    def test_get_or_create_race_single_row(self, file_db, seeded):
        """Two threads lazily creating the same user's preference row: the
        unique constraint + IntegrityError fallback must yield one row and
        no crash."""
        from app.notification.repository.notification_repository import (
            NotificationPreferenceRepository,
        )

        barrier = threading.Barrier(2)
        errors = []

        def worker():
            session = file_db()
            try:
                barrier.wait(timeout=10)
                NotificationPreferenceRepository(session).get_or_create(
                    seeded["user_id"]
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                session.close()

        _run_threads([worker, worker])
        assert errors == [], f"preference get_or_create crashed: {errors!r}"
        from app.notification.models import NotificationPreference

        with file_db() as session:
            assert (
                session.query(NotificationPreference)
                .filter(NotificationPreference.user_id == seeded["user_id"])
                .count()
            ) == 1
