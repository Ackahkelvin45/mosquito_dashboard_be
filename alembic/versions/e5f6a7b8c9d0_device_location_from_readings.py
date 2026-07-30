"""device location comes from incoming readings

Makes latitude/longitude/region nullable (a device can be registered before its
position is known) and adds `community` + `location_updated_at`, which are
filled by reverse-geocoding whatever coordinates the device reports.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'devices'


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c['name'] for c in inspector.get_columns(TABLE)}

    if 'community' not in columns:
        op.add_column(TABLE, sa.Column('community', sa.String(length=150), nullable=True))
    if 'location_updated_at' not in columns:
        op.add_column(TABLE, sa.Column('location_updated_at', sa.DateTime(), nullable=True))

    op.alter_column(TABLE, 'latitude', existing_type=sa.Float(), nullable=True)
    op.alter_column(TABLE, 'longitude', existing_type=sa.Float(), nullable=True)
    op.alter_column(TABLE, 'region', existing_type=sa.String(length=100), nullable=True)
    # These were already Optional in the API schema but NOT NULL in the table,
    # so creating a device without them raised IntegrityError (a 500) instead
    # of being accepted.
    op.alter_column(TABLE, 'description', existing_type=sa.String(length=255), nullable=True)
    op.alter_column(TABLE, 'gmap_link', existing_type=sa.String(length=255), nullable=True)


def downgrade() -> None:
    """Downgrade schema.

    Rows with NULL position/region are backfilled with 0/'' first, otherwise
    restoring NOT NULL would fail.
    """
    op.execute("UPDATE devices SET latitude = 0 WHERE latitude IS NULL")
    op.execute("UPDATE devices SET longitude = 0 WHERE longitude IS NULL")
    op.execute("UPDATE devices SET region = '' WHERE region IS NULL")
    op.execute("UPDATE devices SET description = '' WHERE description IS NULL")
    op.execute("UPDATE devices SET gmap_link = '' WHERE gmap_link IS NULL")

    op.alter_column(TABLE, 'gmap_link', existing_type=sa.String(length=255), nullable=False)
    op.alter_column(TABLE, 'description', existing_type=sa.String(length=255), nullable=False)
    op.alter_column(TABLE, 'region', existing_type=sa.String(length=100), nullable=False)
    op.alter_column(TABLE, 'longitude', existing_type=sa.Float(), nullable=False)
    op.alter_column(TABLE, 'latitude', existing_type=sa.Float(), nullable=False)

    op.drop_column(TABLE, 'location_updated_at')
    op.drop_column(TABLE, 'community')
