"""add api_keys.revoked_by — who revoked a key (owner or super admin)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # create_tables() at app startup may already have created the table with
    # the column (create_all uses the current model).
    columns = {c['name'] for c in inspector.get_columns('api_keys')}
    if 'revoked_by' not in columns:
        op.add_column(
            'api_keys',
            sa.Column('revoked_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('api_keys', 'revoked_by')
