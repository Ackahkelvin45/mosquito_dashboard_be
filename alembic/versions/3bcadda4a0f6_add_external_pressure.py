"""add external_pressure

Revision ID: 3bcadda4a0f6
Revises: 0001_initial_schema
Create Date: 2026-04-06 14:51:48.975704

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3bcadda4a0f6'
down_revision: Union[str, Sequence[str], None] = '0001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'sensor_device_readings' in inspector.get_table_names():
        columns = [c['name'] for c in inspector.get_columns('sensor_device_readings')]
        if 'external_pressure' not in columns:
            op.add_column('sensor_device_readings', sa.Column('external_pressure', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('sensor_device_readings', 'external_pressure')
