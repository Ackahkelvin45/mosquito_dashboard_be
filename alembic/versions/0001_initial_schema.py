"""initial_schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-04-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = inspector.get_table_names()

    # ------------------------------------------------------------------ #
    # Enums                                                                #
    # ------------------------------------------------------------------ #
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(
            "ADMIN", "USER", "SUPER_ADMIN", name="userrole"
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            "PENDING", "APPROVED", "REJECTED", name="approvalstatus"
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            "approved", "rejected", "pending", name="status"
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            "PENDING", "APPROVED", "REJECTED", name="researcherrequeststatus"
        ).create(bind, checkfirst=True)

    # ------------------------------------------------------------------ #
    # users                                                                #
    # ------------------------------------------------------------------ #
    if "users" not in table_names:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("email", sa.String(length=100), nullable=False),
            sa.Column("first_name", sa.String(length=100), nullable=False),
            sa.Column("last_name", sa.String(length=100), nullable=False),
            sa.Column("hashed_password", sa.String(length=255), nullable=False),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "approval_status",
                postgresql.ENUM(
                    "PENDING", "APPROVED", "REJECTED",
                    name="approvalstatus", create_type=False,
                ) if bind.dialect.name == "postgresql" else sa.Enum(
                    "PENDING", "APPROVED", "REJECTED", name="approvalstatus"
                ),
                nullable=False,
                server_default=sa.text("'PENDING'"),
            ),
            sa.Column(
                "role",
                postgresql.ENUM(
                    "ADMIN", "USER", "SUPER_ADMIN",
                    name="userrole", create_type=False,
                ) if bind.dialect.name == "postgresql" else sa.Enum(
                    "ADMIN", "USER", "SUPER_ADMIN", name="userrole"
                ),
                nullable=False,
                server_default=sa.text("'USER'"),
            ),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_users_id", "users", ["id"], unique=False)
        op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ------------------------------------------------------------------ #
    # device_clusters                                                       #
    # ------------------------------------------------------------------ #
    if "device_clusters" not in table_names:
        op.create_table(
            "device_clusters",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("cluster_uuid", sa.String(length=100), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=False),
            sa.Column("password", sa.String(length=255), nullable=False),
            sa.Column(
                "public", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "status",
                postgresql.ENUM(
                    "approved", "rejected", "pending",
                    name="status", create_type=False,
                ) if bind.dialect.name == "postgresql" else sa.Enum(
                    "approved", "rejected", "pending", name="status"
                ),
                nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_device_clusters_id", "device_clusters", ["id"], unique=False)
        op.create_index("ix_device_clusters_name", "device_clusters", ["name"], unique=True)
        op.create_index(
            "ix_device_clusters_cluster_uuid",
            "device_clusters", ["cluster_uuid"], unique=True,
        )

    # ------------------------------------------------------------------ #
    # cluster_admins (many-to-many)                                        #
    # ------------------------------------------------------------------ #
    if "cluster_admins" not in table_names:
        op.create_table(
            "cluster_admins",
            sa.Column(
                "cluster_id", sa.Integer(),
                sa.ForeignKey("device_clusters.id"), nullable=False,
            ),
            sa.Column(
                "user_id", sa.Integer(),
                sa.ForeignKey("users.id"), nullable=False,
            ),
            sa.PrimaryKeyConstraint("cluster_id", "user_id"),
        )

    # ------------------------------------------------------------------ #
    # researcher_requests                                                   #
    # ------------------------------------------------------------------ #
    if "researcher_requests" not in table_names:
        op.create_table(
            "researcher_requests",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "user_id", sa.Integer(),
                sa.ForeignKey("users.id"), nullable=False,
            ),
            sa.Column(
                "cluster_id", sa.Integer(),
                sa.ForeignKey("device_clusters.id"), nullable=True,
            ),
            sa.Column(
                "status",
                postgresql.ENUM(
                    "PENDING", "APPROVED", "REJECTED",
                    name="researcherrequeststatus", create_type=False,
                ) if bind.dialect.name == "postgresql" else sa.Enum(
                    "PENDING", "APPROVED", "REJECTED",
                    name="researcherrequeststatus",
                ),
                nullable=False,
                server_default=sa.text("'PENDING'"),
            ),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id"),
            sa.UniqueConstraint("cluster_id"),
        )
        op.create_index(
            "ix_researcher_requests_id", "researcher_requests", ["id"], unique=False,
        )

    # ------------------------------------------------------------------ #
    # devices                                                              #
    # ------------------------------------------------------------------ #
    if "devices" not in table_names:
        op.create_table(
            "devices",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("device_uuid", sa.String(length=100), nullable=True),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=False),
            sa.Column("longitude", sa.Float(), nullable=False),
            sa.Column("latitude", sa.Float(), nullable=False),
            sa.Column("region", sa.String(length=100), nullable=False),
            sa.Column("gmap_link", sa.String(length=255), nullable=False),
            sa.Column(
                "last_activity", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "total_mosquito_count", sa.Integer(), nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("cluster_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["cluster_id"], ["device_clusters.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_devices_id", "devices", ["id"], unique=False)
        op.create_index("ix_devices_device_uuid", "devices", ["device_uuid"], unique=True)

    # ------------------------------------------------------------------ #
    # sensor_device_readings                                               #
    # ------------------------------------------------------------------ #
    if "sensor_device_readings" not in table_names:
        op.create_table(
            "sensor_device_readings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "device_id", sa.Integer(),
                sa.ForeignKey("devices.id"), nullable=False,
            ),
            sa.Column(
                "timestamp", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("external_temperature", sa.Float(), nullable=True),
            sa.Column("internal_temperature", sa.Float(), nullable=True),
            sa.Column("external_humidity", sa.Float(), nullable=True),
            sa.Column("internal_humidity", sa.Float(), nullable=True),
            sa.Column("internal_pressure", sa.Float(), nullable=True),
            sa.Column("external_pressure", sa.Float(), nullable=True),
            sa.Column("external_light", sa.Float(), nullable=True),
            sa.Column("battery_voltage", sa.Float(), nullable=True),
            sa.Column(
                "trap_status", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_sensor_device_readings_id",
            "sensor_device_readings", ["id"], unique=False,
        )

    # ------------------------------------------------------------------ #
    # mosquito_events                                                       #
    # ------------------------------------------------------------------ #
    if "mosquito_events" not in table_names:
        op.create_table(
            "mosquito_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "device_id", sa.Integer(),
                sa.ForeignKey("devices.id"), nullable=False,
            ),
            sa.Column(
                "timestamp", sa.DateTime(), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "count", sa.Integer(), nullable=False,
                server_default=sa.text("0"),
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_mosquito_events_id", "mosquito_events", ["id"], unique=False)

    # ------------------------------------------------------------------ #
    # mosquito_individual_readings                                          #
    # ------------------------------------------------------------------ #
    if "mosquito_individual_readings" not in table_names:
        op.create_table(
            "mosquito_individual_readings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "batch_id", sa.Integer(),
                sa.ForeignKey("mosquito_events.id"), nullable=False,
            ),
            sa.Column("detection_timestamp", sa.DateTime(), nullable=False),
            sa.Column("species", sa.String(length=250), nullable=True),
            sa.Column("genus", sa.String(length=250), nullable=True),
            sa.Column("age_group", sa.String(length=50), nullable=False),
            sa.Column("sex", sa.String(length=50), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_mosquito_individual_readings_id",
            "mosquito_individual_readings", ["id"], unique=False,
        )


def downgrade() -> None:
    op.drop_table("mosquito_individual_readings")
    op.drop_table("mosquito_events")
    op.drop_table("sensor_device_readings")
    op.drop_table("devices")
    op.drop_table("researcher_requests")
    op.drop_table("cluster_admins")
    op.drop_table("device_clusters")
    op.drop_table("users")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name="researcherrequeststatus").drop(bind, checkfirst=True)
        postgresql.ENUM(name="status").drop(bind, checkfirst=True)
        postgresql.ENUM(name="approvalstatus").drop(bind, checkfirst=True)
        postgresql.ENUM(name="userrole").drop(bind, checkfirst=True)
