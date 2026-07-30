"""add users.cluster_id (the cluster a user belongs to)

Additive step toward cluster-scoped roles: a user can be assigned to a single
cluster at creation. Does NOT touch the existing cluster_admins M2M, roles, or
any data — the full RBAC enforcement/migration is separate.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'users'
COLUMN = 'cluster_id'
FK_NAME = 'fk_users_cluster_id_device_clusters'


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c['name'] for c in inspector.get_columns(TABLE)}
    if COLUMN not in columns:
        op.add_column(TABLE, sa.Column(COLUMN, sa.Integer(), nullable=True))
        op.create_foreign_key(FK_NAME, TABLE, 'device_clusters', [COLUMN], ['id'])
        op.create_index(op.f('ix_users_cluster_id'), TABLE, [COLUMN], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_users_cluster_id'), table_name=TABLE)
    op.drop_constraint(FK_NAME, TABLE, type_='foreignkey')
    op.drop_column(TABLE, COLUMN)
