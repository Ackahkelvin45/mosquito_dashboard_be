"""add password_reset_otps

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-06-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'password_reset_otps' not in inspector.get_table_names():
        op.create_table(
            'password_reset_otps',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('otp_code', sa.String(length=255), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('is_used', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_password_reset_otps_id'), 'password_reset_otps', ['id'], unique=False)
        op.create_index(op.f('ix_password_reset_otps_user_id'), 'password_reset_otps', ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_password_reset_otps_user_id'), table_name='password_reset_otps')
    op.drop_index(op.f('ix_password_reset_otps_id'), table_name='password_reset_otps')
    op.drop_table('password_reset_otps')
