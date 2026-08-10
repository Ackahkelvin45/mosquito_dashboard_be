"""add fields required by MQTT_Schema_Reference.pdf — battery_pct,
esp1_link_alive on sensor readings; p_mosq, binary_decision, taxon_probs,
sex_probs, inference_ms on mosquito individual readings

Revision ID: 8c3d16a4aad5
Revises: f2a3b4c5d6e7
Create Date: 2026-08-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8c3d16a4aad5'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    sensor_columns = {c['name'] for c in inspector.get_columns('sensor_device_readings')}
    if 'battery_pct' not in sensor_columns:
        op.add_column('sensor_device_readings', sa.Column('battery_pct', sa.Integer(), nullable=True))
    if 'esp1_link_alive' not in sensor_columns:
        op.add_column('sensor_device_readings', sa.Column('esp1_link_alive', sa.Boolean(), nullable=True))

    mosquito_columns = {c['name'] for c in inspector.get_columns('mosquito_individual_readings')}
    if 'p_mosq' not in mosquito_columns:
        op.add_column('mosquito_individual_readings', sa.Column('p_mosq', sa.Float(), nullable=True))
    if 'binary_decision' not in mosquito_columns:
        op.add_column('mosquito_individual_readings', sa.Column('binary_decision', sa.Boolean(), nullable=True))
    if 'taxon_probs' not in mosquito_columns:
        op.add_column('mosquito_individual_readings', sa.Column('taxon_probs', sa.JSON(), nullable=True))
    if 'sex_probs' not in mosquito_columns:
        op.add_column('mosquito_individual_readings', sa.Column('sex_probs', sa.JSON(), nullable=True))
    if 'inference_ms' not in mosquito_columns:
        op.add_column('mosquito_individual_readings', sa.Column('inference_ms', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('mosquito_individual_readings', 'inference_ms')
    op.drop_column('mosquito_individual_readings', 'sex_probs')
    op.drop_column('mosquito_individual_readings', 'taxon_probs')
    op.drop_column('mosquito_individual_readings', 'binary_decision')
    op.drop_column('mosquito_individual_readings', 'p_mosq')
    op.drop_column('sensor_device_readings', 'esp1_link_alive')
    op.drop_column('sensor_device_readings', 'battery_pct')
