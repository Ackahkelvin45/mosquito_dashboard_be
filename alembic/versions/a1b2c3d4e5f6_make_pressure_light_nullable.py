"""make internal_pressure and external_light nullable

Revision ID: a1b2c3d4e5f6
Revises: 3bcadda4a0f6
Create Date: 2026-05-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '3bcadda4a0f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('sensor_device_readings', 'internal_pressure',
                    existing_type=sa.Float(),
                    nullable=True)
    op.alter_column('sensor_device_readings', 'external_light',
                    existing_type=sa.Float(),
                    nullable=True)


def downgrade() -> None:
    op.alter_column('sensor_device_readings', 'internal_pressure',
                    existing_type=sa.Float(),
                    nullable=False)
    op.alter_column('sensor_device_readings', 'external_light',
                    existing_type=sa.Float(),
                    nullable=False)
