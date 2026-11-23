"""add notifications.acknowledged_at / acknowledged_by_user_id

Alert status opened -> acknowledged -> closed (closed = the existing
handled_at). Shared across the alert_group like handled. Additive only:
existing rows keep NULL (no backfill) — an alert closed before this column
existed was never acknowledged as far as anyone measured.

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-11-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6f7a8b9c0d1e'
down_revision: Union[str, Sequence[str], None] = '5e6f7a8b9c0d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'notifications'
FK_NAME = 'fk_notifications_acknowledged_by_user_id_users'


def upgrade() -> None:
    """Upgrade schema."""
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        if 'acknowledged_at' not in columns:
            batch.add_column(sa.Column('acknowledged_at', sa.DateTime(), nullable=True))
        if 'acknowledged_by_user_id' not in columns:
            batch.add_column(sa.Column('acknowledged_by_user_id', sa.Integer(), nullable=True))
            batch.create_foreign_key(FK_NAME, 'users', ['acknowledged_by_user_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(FK_NAME, type_='foreignkey')
        batch.drop_column('acknowledged_by_user_id')
        batch.drop_column('acknowledged_at')
