"""cluster_admins_many_to_many

Revision ID: c3337d90d5f1
Revises: e22bc58ecb51
Create Date: 2026-03-30 10:46:39.685658

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c3337d90d5f1'
down_revision: Union[str, Sequence[str], None] = 'e22bc58ecb51'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # NOTE:
    # This revision contains destructive DDL that drops core tables.
    # It is intentionally a no-op to avoid accidental data loss.
    pass


def downgrade() -> None:
    """Downgrade schema."""
    # No-op; see upgrade() note above.
    pass
