"""add data_imports + import_batch_id provenance

Stakeholder CSV imports (POST /imports/*). Each batch is one data_imports row;
rows it creates carry import_batch_id (NULL = device-sent). No backfill.

Revision ID: 3c4d5e6f7a8b
Revises: f9a0b1c2d3e4
Create Date: 2026-11-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3c4d5e6f7a8b'
down_revision: Union[str, Sequence[str], None] = 'f9a0b1c2d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'data_imports'
ROW_TABLES = ('sensor_device_readings', 'mosquito_events')


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    # create_tables() at app startup may already have created the table/columns.
    if TABLE not in inspector.get_table_names():
        op.create_table(
            TABLE,
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('kind', sa.String(length=20), nullable=False),
            sa.Column('filename', sa.String(length=255), nullable=True),
            sa.Column('cluster_id', sa.Integer(), nullable=True),
            sa.Column('rows_total', sa.Integer(), nullable=False),
            sa.Column('rows_imported', sa.Integer(), nullable=False),
            sa.Column('rows_skipped', sa.Integer(), nullable=False),
            sa.Column('imported_by_user_id', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['imported_by_user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_data_imports_id'), TABLE, ['id'], unique=False)
        op.create_index(op.f('ix_data_imports_cluster_id'), TABLE, ['cluster_id'], unique=False)

    for table in ROW_TABLES:
        if 'import_batch_id' in {c['name'] for c in inspector.get_columns(table)}:
            continue
        op.add_column(table, sa.Column('import_batch_id', sa.Integer(), nullable=True))
        op.create_foreign_key(f'fk_{table}_import_batch_id', table, TABLE, ['import_batch_id'], ['id'])
        op.create_index(op.f(f'ix_{table}_import_batch_id'), table, ['import_batch_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    for table in ROW_TABLES:
        op.drop_index(op.f(f'ix_{table}_import_batch_id'), table_name=table)
        op.drop_constraint(f'fk_{table}_import_batch_id', table, type_='foreignkey')
        op.drop_column(table, 'import_batch_id')
    op.drop_index(op.f('ix_data_imports_cluster_id'), table_name=TABLE)
    op.drop_index(op.f('ix_data_imports_id'), table_name=TABLE)
    op.drop_table(TABLE)
