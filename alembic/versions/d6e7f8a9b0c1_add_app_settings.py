"""add app_settings — small persistent app-wide switches; first use is the
super-admin master gate for 2FA sign-in (two_factor_login_enabled). Absent
row = enabled, so deployments keep today's behaviour.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-09-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, Sequence[str], None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'app_settings' not in inspector.get_table_names():
        op.create_table(
            'app_settings',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('name', sa.String(50), nullable=False, unique=True, index=True),
            sa.Column('value', sa.String(100), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.Column('updated_by', sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('app_settings')
