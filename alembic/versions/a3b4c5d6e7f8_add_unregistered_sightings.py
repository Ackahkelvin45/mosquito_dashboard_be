"""add unregistered_device_sightings — MQTT data from unknown UUIDs was
silently dropped (TODO.md §1); one upserted row per stray UUID turns that
into a visible registration prompt with location prefill.

Revision ID: a3b4c5d6e7f8
Revises: f0a1b2c3d4e5
Create Date: 2026-08-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, Sequence[str], None] = 'f0a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'unregistered_device_sightings' not in inspector.get_table_names():
        op.create_table(
            'unregistered_device_sightings',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('device_uuid', sa.String(100), nullable=False, unique=True, index=True),
            sa.Column('first_seen', sa.DateTime(), nullable=False),
            sa.Column('last_seen', sa.DateTime(), nullable=False),
            sa.Column('message_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_topic', sa.String(255), nullable=True),
            sa.Column('last_payload', sa.JSON(), nullable=True),
            sa.Column('latitude', sa.Float(), nullable=True),
            sa.Column('longitude', sa.Float(), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('unregistered_device_sightings')
