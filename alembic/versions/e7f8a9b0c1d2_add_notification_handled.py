"""add notifications.alert_group / handled_at / handled_by_user_id

alert_group links the per-recipient rows of one emission so "handled" is
shared by every recipient. Additive only: existing rows keep NULL (no
backfill) and are handled per-row.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-11-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, Sequence[str], None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'notifications'
FK_NAME = 'fk_notifications_handled_by_user_id_users'


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # create_tables() at app startup may already have added the columns.
    columns = {c['name'] for c in inspector.get_columns(TABLE)}
    if 'alert_group' not in columns:
        op.add_column(TABLE, sa.Column('alert_group', sa.String(length=36), nullable=True))
        op.create_index(op.f('ix_notifications_alert_group'), TABLE, ['alert_group'], unique=False)
    if 'handled_at' not in columns:
        op.add_column(TABLE, sa.Column('handled_at', sa.DateTime(), nullable=True))
    if 'handled_by_user_id' not in columns:
        op.add_column(TABLE, sa.Column('handled_by_user_id', sa.Integer(), nullable=True))
        op.create_foreign_key(FK_NAME, TABLE, 'users', ['handled_by_user_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(FK_NAME, TABLE, type_='foreignkey')
    op.drop_column(TABLE, 'handled_by_user_id')
    op.drop_column(TABLE, 'handled_at')
    op.drop_index(op.f('ix_notifications_alert_group'), table_name=TABLE)
    op.drop_column(TABLE, 'alert_group')
