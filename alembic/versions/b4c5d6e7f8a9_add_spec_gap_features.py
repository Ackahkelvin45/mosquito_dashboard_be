"""FR-4 / FR-18 / FR-27 spec-gap features: email-OTP 2FA (+ session
versioning and reset-OTP attempt caps), DB-backed alert thresholds with
personal overrides, and the audit log.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-08-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, Sequence[str], None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table):
    return {c['name'] for c in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ── FR-4: 2FA + session versioning ───────────────────────────────────────
    user_cols = _columns(inspector, 'users')
    if 'two_factor_enabled' not in user_cols:
        op.add_column('users', sa.Column('two_factor_enabled', sa.Boolean(),
                                         nullable=False, server_default=sa.false()))
    if 'token_version' not in user_cols:
        op.add_column('users', sa.Column('token_version', sa.Integer(),
                                         nullable=False, server_default='0'))

    if 'attempts' not in _columns(inspector, 'password_reset_otps'):
        op.add_column('password_reset_otps',
                      sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))

    if 'login_otps' not in tables:
        op.create_table(
            'login_otps',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'),
                      nullable=False, index=True),
            sa.Column('code_hash', sa.String(255), nullable=False),
            sa.Column('challenge_hash', sa.String(64), nullable=False,
                      unique=True, index=True),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('is_used', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('last_sent_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
        )

    # ── FR-27: audit log ─────────────────────────────────────────────────────
    if 'audit_logs' not in tables:
        op.create_table(
            'audit_logs',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('occurred_at', sa.DateTime(), nullable=False, index=True),
            sa.Column('actor_user_id', sa.Integer(), nullable=True, index=True),
            sa.Column('actor_email', sa.String(120), nullable=True),
            sa.Column('action', sa.String(50), nullable=False, index=True),
            sa.Column('target_type', sa.String(30), nullable=True),
            sa.Column('target_id', sa.String(100), nullable=True),
            sa.Column('detail', sa.JSON(), nullable=True),
            sa.Column('ip', sa.String(64), nullable=True),
        )

    # ── FR-18: thresholds ────────────────────────────────────────────────────
    if 'alert_settings' not in tables:
        op.create_table(
            'alert_settings',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('name', sa.String(50), nullable=False, unique=True, index=True),
            sa.Column('value', sa.Float(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.Column('updated_by', sa.Integer(), nullable=True),
        )

    preference_cols = _columns(inspector, 'notification_preferences')
    additions = [
        ('surge_alerts', sa.Column('surge_alerts', sa.Boolean(),
                                   nullable=False, server_default=sa.true())),
        ('environment_alerts', sa.Column('environment_alerts', sa.Boolean(),
                                         nullable=False, server_default=sa.true())),
        ('personal_temp_max', sa.Column('personal_temp_max', sa.Float(), nullable=True)),
        ('personal_humidity_max', sa.Column('personal_humidity_max', sa.Float(), nullable=True)),
        ('personal_battery_min_v', sa.Column('personal_battery_min_v', sa.Float(), nullable=True)),
        ('personal_surge_threshold', sa.Column('personal_surge_threshold', sa.Integer(), nullable=True)),
    ]
    for name, column in additions:
        if name not in preference_cols:
            op.add_column('notification_preferences', column)


def downgrade() -> None:
    """Downgrade schema."""
    for name in ('personal_surge_threshold', 'personal_battery_min_v',
                 'personal_humidity_max', 'personal_temp_max',
                 'environment_alerts', 'surge_alerts'):
        op.drop_column('notification_preferences', name)
    op.drop_table('alert_settings')
    op.drop_table('audit_logs')
    op.drop_table('login_otps')
    op.drop_column('password_reset_otps', 'attempts')
    op.drop_column('users', 'token_version')
    op.drop_column('users', 'two_factor_enabled')
