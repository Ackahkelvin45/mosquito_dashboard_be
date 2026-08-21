"""add MQTT monitoring — hourly traffic aggregates + raw ingest-error log for
the System Health page, and the PIPELINE_SILENT notification type for the
fleet-silence watchdog.

Revision ID: f0a1b2c3d4e5
Revises: d8e9f0a1b2c3
Create Date: 2026-08-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f0a1b2c3d4e5'
down_revision: Union[str, Sequence[str], None] = 'd8e9f0a1b2c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'mqtt_traffic_hourly' not in tables:
        op.create_table(
            'mqtt_traffic_hourly',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('bucket_start', sa.DateTime(), nullable=False, index=True),
            sa.Column('device_id', sa.Integer(),
                      sa.ForeignKey('devices.id'), nullable=False, index=True),
            sa.Column('event_type', sa.String(20), nullable=False),
            sa.Column('is_test', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('message_count', sa.Integer(), nullable=False, server_default='0'),
            sa.UniqueConstraint('bucket_start', 'device_id', 'event_type', 'is_test',
                                name='uq_mqtt_traffic_bucket'),
        )

    if 'mqtt_ingest_errors' not in tables:
        op.create_table(
            'mqtt_ingest_errors',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('occurred_at', sa.DateTime(), nullable=False, index=True),
            sa.Column('error_type', sa.String(30), nullable=False, index=True),
            sa.Column('topic', sa.String(255), nullable=True),
            sa.Column('device_uuid', sa.String(100), nullable=True),
            sa.Column('detail', sa.String(500), nullable=True),
        )

    # PIPELINE_SILENT joins the notificationtype enum (PG only — SQLite dev
    # DBs store enums as VARCHAR via create_all and need nothing here).
    if bind.dialect.name == 'postgresql':
        op.execute("ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS 'PIPELINE_SILENT'")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('mqtt_ingest_errors')
    op.drop_table('mqtt_traffic_hourly')
    # PG enum values can't be removed without rebuilding the type; the extra
    # value is harmless, so downgrade leaves it in place.
