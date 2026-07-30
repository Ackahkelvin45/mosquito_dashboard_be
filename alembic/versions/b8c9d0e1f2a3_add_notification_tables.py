"""add notification tables

Notification system: notifications (fan-out on write, one row per recipient),
push_subscriptions (one per browser/device), notification_preferences (per-user
toggles), notification_deliveries (per-channel attempt tracking), plus the
nullable devices.offline_since state marker for the offline/online detector.

Idempotent: every create is guarded with an inspector check because
create_tables() (Base.metadata.create_all) may already have created these in a
dev database.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-30 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SEVERITY_VALUES = ("INFO", "SUCCESS", "WARNING", "CRITICAL")
CATEGORY_VALUES = ("MOSQUITO", "SENSOR", "DEVICE", "ACCOUNT", "RESEARCH", "CLUSTER", "SYSTEM")
TYPE_VALUES = (
    "SPECIES_DETECTED", "ACTIVITY_SURGE", "LOW_BATTERY", "TRAP_TRIGGERED",
    "EXTREME_TEMPERATURE", "EXTREME_HUMIDITY", "SENSOR_MALFUNCTION",
    "UNKNOWN_DEVICE", "INVALID_PAYLOAD", "DEVICE_OFFLINE", "DEVICE_ONLINE",
    "DEVICE_LOCATION_CHANGED", "DEVICE_REASSIGNED", "USER_REGISTERED",
    "USER_APPROVED", "USER_REJECTED", "ROLE_CHANGED", "PASSWORD_RESET",
    "RESEARCHER_REQUEST_SUBMITTED", "RESEARCHER_REQUEST_APPROVED",
    "RESEARCHER_REQUEST_REJECTED", "CLUSTER_CREATED", "CLUSTER_UPDATED",
    "CLUSTER_DEVICE_ADDED", "CLUSTER_DEVICE_REMOVED", "MAINTENANCE_DUE",
    "DAILY_SUMMARY", "WEEKLY_SUMMARY", "TEST",
)
PROVIDER_VALUES = ("WEBPUSH", "FCM", "APNS")
CHANNEL_VALUES = ("PUSH", "EMAIL")
STATUS_VALUES = ("PENDING", "SENT", "FAILED")


def _enum(bind, name: str, values: tuple):
    """Enum column type matching how models/0001 declare enums: the pg type is
    created separately (checkfirst) and referenced with create_type=False."""
    if bind.dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = inspector.get_table_names()

    # ------------------------------------------------------------------ #
    # Enums                                                               #
    # ------------------------------------------------------------------ #
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(*SEVERITY_VALUES, name="notificationseverity").create(bind, checkfirst=True)
        postgresql.ENUM(*CATEGORY_VALUES, name="notificationcategory").create(bind, checkfirst=True)
        postgresql.ENUM(*TYPE_VALUES, name="notificationtype").create(bind, checkfirst=True)
        postgresql.ENUM(*PROVIDER_VALUES, name="pushprovider").create(bind, checkfirst=True)
        postgresql.ENUM(*CHANNEL_VALUES, name="deliverychannel").create(bind, checkfirst=True)
        postgresql.ENUM(*STATUS_VALUES, name="deliverystatus").create(bind, checkfirst=True)

    # ------------------------------------------------------------------ #
    # notifications                                                       #
    # ------------------------------------------------------------------ #
    if "notifications" not in table_names:
        op.create_table(
            "notifications",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("cluster_id", sa.Integer(), nullable=True),
            sa.Column("device_id", sa.Integer(), nullable=True),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("body", sa.String(length=1000), nullable=False),
            sa.Column("notification_type", _enum(bind, "notificationtype", TYPE_VALUES), nullable=False),
            sa.Column(
                "severity", _enum(bind, "notificationseverity", SEVERITY_VALUES),
                nullable=False, server_default=sa.text("'INFO'"),
            ),
            sa.Column(
                "category", _enum(bind, "notificationcategory", CATEGORY_VALUES),
                nullable=False, server_default=sa.text("'SYSTEM'"),
            ),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("icon", sa.String(length=50), nullable=True),
            sa.Column("action_url", sa.String(length=255), nullable=True),
            sa.Column("dedupe_key", sa.String(length=255), nullable=True),
            sa.Column("read_at", sa.DateTime(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column("archived_at", sa.DateTime(), nullable=True),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.Column("scheduled_for", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["cluster_id"], ["device_clusters.id"]),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_notifications_id", "notifications", ["id"], unique=False)
        op.create_index("ix_notifications_user_id", "notifications", ["user_id"], unique=False)
        op.create_index("ix_notifications_cluster_id", "notifications", ["cluster_id"], unique=False)
        op.create_index("ix_notifications_device_id", "notifications", ["device_id"], unique=False)
        op.create_index("ix_notifications_dedupe_key", "notifications", ["dedupe_key"], unique=False)
        # Composite indexes for the hot paths: per-user list (newest first —
        # b-tree scans backwards fine), unread badge, dedupe window lookup.
        op.create_index(
            "ix_notifications_user_id_created_at", "notifications",
            ["user_id", "created_at"], unique=False,
        )
        op.create_index(
            "ix_notifications_user_id_read_at", "notifications",
            ["user_id", "read_at"], unique=False,
        )
        op.create_index(
            "ix_notifications_dedupe_key_created_at", "notifications",
            ["dedupe_key", "created_at"], unique=False,
        )

    # ------------------------------------------------------------------ #
    # push_subscriptions                                                  #
    # ------------------------------------------------------------------ #
    if "push_subscriptions" not in table_names:
        op.create_table(
            "push_subscriptions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("endpoint", sa.String(length=500), nullable=False),
            sa.Column("p256dh", sa.String(length=255), nullable=True),
            sa.Column("auth", sa.String(length=255), nullable=True),
            sa.Column(
                "provider", _enum(bind, "pushprovider", PROVIDER_VALUES),
                nullable=False, server_default=sa.text("'WEBPUSH'"),
            ),
            sa.Column("browser", sa.String(length=50), nullable=True),
            sa.Column("platform", sa.String(length=50), nullable=True),
            sa.Column("device_name", sa.String(length=100), nullable=True),
            sa.Column(
                "active", sa.Boolean(), nullable=False, server_default=sa.true(),
            ),
            sa.Column(
                "failure_count", sa.Integer(), nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_push_subscriptions_id", "push_subscriptions", ["id"], unique=False)
        op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"], unique=False)
        op.create_index("ix_push_subscriptions_endpoint", "push_subscriptions", ["endpoint"], unique=True)

    # ------------------------------------------------------------------ #
    # notification_preferences                                            #
    # ------------------------------------------------------------------ #
    if "notification_preferences" not in table_names:
        op.create_table(
            "notification_preferences",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("species_alerts", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("battery_alerts", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("offline_alerts", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("admin_alerts", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("researcher_alerts", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("push_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("in_app_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_notification_preferences_id", "notification_preferences", ["id"], unique=False)
        op.create_index(
            "ix_notification_preferences_user_id", "notification_preferences",
            ["user_id"], unique=True,
        )

    # ------------------------------------------------------------------ #
    # notification_deliveries                                             #
    # ------------------------------------------------------------------ #
    if "notification_deliveries" not in table_names:
        op.create_table(
            "notification_deliveries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("notification_id", sa.Integer(), nullable=False),
            sa.Column("channel", _enum(bind, "deliverychannel", CHANNEL_VALUES), nullable=False),
            sa.Column(
                "status", _enum(bind, "deliverystatus", STATUS_VALUES),
                nullable=False, server_default=sa.text("'PENDING'"),
            ),
            sa.Column(
                "attempts", sa.Integer(), nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
            sa.Column("error", sa.String(length=500), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_notification_deliveries_id", "notification_deliveries", ["id"], unique=False)
        op.create_index(
            "ix_notification_deliveries_notification_id", "notification_deliveries",
            ["notification_id"], unique=False,
        )

    # ------------------------------------------------------------------ #
    # devices.offline_since (state marker for the offline detector)       #
    # ------------------------------------------------------------------ #
    device_columns = {c["name"] for c in inspector.get_columns("devices")}
    if "offline_since" not in device_columns:
        op.add_column("devices", sa.Column("offline_since", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("devices", "offline_since")

    op.drop_index("ix_notification_deliveries_notification_id", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_id", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")

    op.drop_index("ix_notification_preferences_user_id", table_name="notification_preferences")
    op.drop_index("ix_notification_preferences_id", table_name="notification_preferences")
    op.drop_table("notification_preferences")

    op.drop_index("ix_push_subscriptions_endpoint", table_name="push_subscriptions")
    op.drop_index("ix_push_subscriptions_user_id", table_name="push_subscriptions")
    op.drop_index("ix_push_subscriptions_id", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")

    op.drop_index("ix_notifications_dedupe_key_created_at", table_name="notifications")
    op.drop_index("ix_notifications_user_id_read_at", table_name="notifications")
    op.drop_index("ix_notifications_user_id_created_at", table_name="notifications")
    op.drop_index("ix_notifications_dedupe_key", table_name="notifications")
    op.drop_index("ix_notifications_device_id", table_name="notifications")
    op.drop_index("ix_notifications_cluster_id", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_index("ix_notifications_id", table_name="notifications")
    op.drop_table("notifications")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name="deliverystatus").drop(bind, checkfirst=True)
        postgresql.ENUM(name="deliverychannel").drop(bind, checkfirst=True)
        postgresql.ENUM(name="pushprovider").drop(bind, checkfirst=True)
        postgresql.ENUM(name="notificationtype").drop(bind, checkfirst=True)
        postgresql.ENUM(name="notificationcategory").drop(bind, checkfirst=True)
        postgresql.ENUM(name="notificationseverity").drop(bind, checkfirst=True)
