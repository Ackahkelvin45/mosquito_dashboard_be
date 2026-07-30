"""drop device_clusters.password

Cluster access is no longer password-gated: clusters are created with just a
name, description, and public flag, and access comes from being assigned to
the cluster (users.cluster_id) or being a cluster admin.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'device_clusters'
COLUMN = 'password'


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c['name'] for c in inspector.get_columns(TABLE)}
    if COLUMN in columns:
        op.drop_column(TABLE, COLUMN)


def downgrade() -> None:
    """Downgrade schema."""
    # Nullable on the way back: the passwords are gone and existing rows
    # can't be backfilled.
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=255), nullable=True))
