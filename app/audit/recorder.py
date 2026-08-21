"""audit() — the one way rows enter the audit log.

Contract (mirrors emit() in app/notification/events.py): NEVER raises, and
COMMITS ITSELF. Self-commit is not a style choice — get_db never commits, so
an audit row left pending is rolled back the moment the route raises
(exactly the failure paths an audit log exists to capture). Call audit()
BEFORE raising on every failure path.
"""
import logging

from sqlalchemy.orm import Session

from app.audit.models import AuditAction, AuditLog

logger = logging.getLogger(__name__)


def audit(
    session: Session,
    action: AuditAction,
    *,
    actor_user_id: int | None = None,
    actor_email: str | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    detail: dict | None = None,
    ip: str | None = None,
) -> None:
    try:
        session.add(AuditLog(
            actor_user_id=actor_user_id,
            actor_email=(actor_email or None) and str(actor_email)[:120],
            action=str(AuditAction(action)),
            target_type=(target_type or None) and str(target_type)[:30],
            target_id=None if target_id is None else str(target_id)[:100],
            detail=detail,
            ip=(ip or None) and str(ip)[:64],
        ))
        session.commit()
    except Exception:
        logger.exception("Audit write failed for action %s", action)
        # Only roll back a poisoned session — an unconditional rollback would
        # discard the caller's own uncommitted work (see events.py emit()).
        try:
            if not session.is_active:
                session.rollback()
        except Exception:
            logger.exception("Rollback after failed audit write also failed")
