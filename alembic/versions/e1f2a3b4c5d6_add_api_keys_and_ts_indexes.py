"""add api_keys table + (timestamp, id) indexes for the public data API

The public API pages the two high-volume tables by keyset on (timestamp, id);
the existing (device_id, timestamp) composites can't serve a fleet-wide
ORDER BY timestamp, so each table gets a (timestamp, id) index.

Revision ID: e1f2a3b4c5d6
Revises: c9d0e1f2a3b4
Create Date: 2026-08-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS_INDEXES = [
    ('ix_sensor_device_readings_timestamp_id', 'sensor_device_readings'),
    ('ix_mosquito_events_timestamp_id', 'mosquito_events'),
]


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # create_tables() may already have created the table via create_all.
    if 'api_keys' not in inspector.get_table_names():
        op.create_table(
            'api_keys',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('description', sa.String(length=255), nullable=True),
            sa.Column('key_prefix', sa.String(length=12), nullable=False),
            sa.Column('key_hash', sa.String(length=64), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('last_used_at', sa.DateTime(), nullable=True),
            sa.Column('total_requests', sa.Integer(), nullable=False),
        )
        op.create_index('ix_api_keys_id', 'api_keys', ['id'])
        op.create_index('ix_api_keys_key_hash', 'api_keys', ['key_hash'], unique=True)
        op.create_index('ix_api_keys_user_id', 'api_keys', ['user_id'])

    for index_name, table_name in TS_INDEXES:
        existing = {ix['name'] for ix in inspector.get_indexes(table_name)}
        if index_name not in existing:
            op.create_index(index_name, table_name, ['timestamp', 'id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    for index_name, table_name in TS_INDEXES:
        op.drop_index(index_name, table_name=table_name)
    op.drop_table('api_keys')
