"""add admin exclusion flag to readings/events

excluded_at / excluded_by_user_id / exclusion_reason: an admin can flag a bad
sensor reading or mosquito event as excluded from analysis. Flag only — the
row's values are never altered. Nullable, no backfill (NULL = included).

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-11-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e6f7a8b9c0d'
down_revision: Union[str, Sequence[str], None] = '4d5e6f7a8b9c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ('sensor_device_readings', 'mosquito_events')


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    for table in TABLES:
        # Harmless guard (re-runs, hand-patched DBs). create_all() never adds
        # columns to an existing table, so it is not what this protects.
        existing = {c['name'] for c in inspector.get_columns(table)}
        if 'excluded_at' not in existing:
            op.add_column(table, sa.Column('excluded_at', sa.DateTime(), nullable=True))
        if 'excluded_by_user_id' not in existing:
            op.add_column(table, sa.Column('excluded_by_user_id', sa.Integer(), nullable=True))
            op.create_foreign_key(f'fk_{table}_excluded_by_user_id', table, 'users',
                                  ['excluded_by_user_id'], ['id'])
        if 'exclusion_reason' not in existing:
            op.add_column(table, sa.Column('exclusion_reason', sa.String(length=500), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for table in TABLES:
        # Dropping the column drops its FK too, whatever the constraint is named.
        op.drop_column(table, 'exclusion_reason')
        op.drop_column(table, 'excluded_by_user_id')
        op.drop_column(table, 'excluded_at')
