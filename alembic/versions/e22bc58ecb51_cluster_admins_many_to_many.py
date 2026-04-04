"""cluster_admins_many_to_many

Revision ID: e22bc58ecb51
Revises: 6c6f49965b0d
Create Date: 2026-03-30 09:46:24.102080

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e22bc58ecb51'
down_revision: Union[str, Sequence[str], None] = '6c6f49965b0d'
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
