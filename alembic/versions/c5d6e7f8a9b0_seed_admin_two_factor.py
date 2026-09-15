"""2FA becomes a per-user choice: the role-based mandate for admins is
removed from code, so existing ADMIN/SUPER_ADMIN accounts are seeded with
two_factor_enabled=true to preserve their current protection — each of them
can now turn it off themselves in Settings.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-09-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, Sequence[str], None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "UPDATE users SET two_factor_enabled = true "
        "WHERE role IN ('ADMIN', 'SUPER_ADMIN')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # No-op: we can't know which admins had opted in on their own.
    pass
