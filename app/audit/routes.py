"""Audit-log read API (FR-27) — SUPER_ADMIN only, SQL-paginated (the table
grows without bound, so no load-all-and-slice here)."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.audit.models import AuditAction, AuditLog
from app.core.database import get_db
from app.core.pagination import DEFAULT_PAGE, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.core.security.permissions import require_super_admin

router = APIRouter(dependencies=[Depends(require_super_admin)])


class AuditLogResponse(BaseModel):
    id: int
    occurred_at: datetime
    actor_user_id: Optional[int] = None
    actor_email: Optional[str] = None
    action: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    detail: Optional[dict] = None
    ip: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


@router.get("", response_model=Page[AuditLogResponse])
def list_audit_logs(
    page: int = Query(default=DEFAULT_PAGE, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    action: Optional[AuditAction] = Query(default=None),
    actor_email: Optional[str] = Query(default=None, description="Substring match"),
    start_date: Optional[datetime] = Query(default=None),
    end_date: Optional[datetime] = Query(default=None),
    session: Session = Depends(get_db),
):
    q = session.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action == str(action))
    if actor_email:
        q = q.filter(AuditLog.actor_email.ilike(f"%{actor_email}%"))
    if start_date:
        q = q.filter(AuditLog.occurred_at >= start_date)
    if end_date:
        q = q.filter(AuditLog.occurred_at <= end_date)

    total = q.count()
    rows = (
        q.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return Page(
        items=[AuditLogResponse.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size, total_pages=total_pages,
    )


@router.get("/actions", response_model=list[str])
def list_audit_actions():
    """The filter dropdown's option list — kept server-side so it can't drift."""
    return [a.value for a in AuditAction]
