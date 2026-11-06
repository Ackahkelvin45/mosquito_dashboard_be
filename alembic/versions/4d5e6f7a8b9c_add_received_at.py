"""add received_at (server receive time) to readings/events

Server UTC time a row arrived from the device, to measure how late data
reaches the server. Nullable, no backfill, no server_default: existing rows
and CSV imports stay NULL ("not measured").

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-11-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4d5e6f7a8b9c'
down_revision: Union[str, Sequence[str], None] = '3c4d5e6f7a8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ('sensor_device_readings', 'mosquito_events')


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    for table in TABLES:
        # create_tables() at app startup may already have added the column.
        if 'received_at' in {c['name'] for c in inspector.get_columns(table)}:
            continue
        op.add_column(table, sa.Column('received_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for table in TABLES:
        op.drop_column(table, 'received_at')
