"""add devices.last_sensor_data_at — liveness heartbeat stamped only by
periodic sensor_data messages, so sporadic mosquito_data events can't mask a
dead telemetry loop. Drives the is_active badge and the offline-detection job.

Revision ID: d8e9f0a1b2c3
Revises: b7e4f91c2a08
Create Date: 2026-08-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8e9f0a1b2c3'
down_revision: Union[str, Sequence[str], None] = 'b7e4f91c2a08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    device_columns = {c['name'] for c in inspector.get_columns('devices')}
    if 'last_sensor_data_at' not in device_columns:
        op.add_column(
            'devices',
            sa.Column('last_sensor_data_at', sa.DateTime(), nullable=True),
        )
        # Best-effort backfill: before this column existed, last_activity was
        # in practice kept fresh by sensor_data (the periodic message), so it
        # is the closest available approximation. Without it every device
        # would flip to "never reported" and the offline job would go blind
        # until the next heartbeat.
        op.execute('UPDATE devices SET last_sensor_data_at = last_activity')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('devices', 'last_sensor_data_at')
