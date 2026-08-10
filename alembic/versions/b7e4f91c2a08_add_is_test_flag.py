"""add is_test flag — device Test-mode data arrives on "_test"-suffixed MQTT
topics (MQTT_Schema_Reference.pdf) and must be distinguishable from live data

Revision ID: b7e4f91c2a08
Revises: 8c3d16a4aad5
Create Date: 2026-08-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e4f91c2a08'
down_revision: Union[str, Sequence[str], None] = '8c3d16a4aad5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    sensor_columns = {c['name'] for c in inspector.get_columns('sensor_device_readings')}
    if 'is_test' not in sensor_columns:
        op.add_column(
            'sensor_device_readings',
            sa.Column('is_test', sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    mosquito_columns = {c['name'] for c in inspector.get_columns('mosquito_events')}
    if 'is_test' not in mosquito_columns:
        op.add_column(
            'mosquito_events',
            sa.Column('is_test', sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('mosquito_events', 'is_test')
    op.drop_column('sensor_device_readings', 'is_test')
