"""sync researcher request schema

Revision ID: 6dc280d13de3
Revises: 87696266ee52
Create Date: 2026-03-31

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "6dc280d13de3"
down_revision: Union[str, Sequence[str], None] = "87696266ee52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(c["name"] == column_name for c in inspector.get_columns(table_name))


def _has_fk_on_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    for fk in inspector.get_foreign_keys(table_name):
        if column_name in fk.get("constrained_columns", []):
            return True
    return False


def _has_unique_on_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    for uc in inspector.get_unique_constraints(table_name):
        if uc.get("column_names") == [column_name]:
            return True
    return False


def _has_index(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    for idx in inspector.get_indexes(table_name):
        if idx.get("name") == index_name:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # --- Create users table if it doesn't exist ---
    # The users table was never generated in any prior migration. We create it
    # here (idempotently) so that the FK from researcher_requests -> users works.
    if not _has_table(inspector, "users"):
        if bind.dialect.name == "postgresql":
            # The approvalstatus/userrole enums were originally created with
            # UPPERCASE values — match that casing here.
            postgresql.ENUM(
                "ADMIN", "USER", "SUPER_ADMIN", name="userrole"
            ).create(bind, checkfirst=True)
            postgresql.ENUM(
                "PENDING", "APPROVED", "REJECTED", name="approvalstatus"
            ).create(bind, checkfirst=True)

        # Use create_type=False so SQLAlchemy doesn't try to CREATE the
        # enum types again (we already ensured they exist above).
        approval_status_col = postgresql.ENUM(
            "PENDING", "APPROVED", "REJECTED",
            name="approvalstatus", create_type=False,
        ) if bind.dialect.name == "postgresql" else sa.Enum(
            "PENDING", "APPROVED", "REJECTED", name="approvalstatus"
        )
        role_col = postgresql.ENUM(
            "ADMIN", "USER", "SUPER_ADMIN",
            name="userrole", create_type=False,
        ) if bind.dialect.name == "postgresql" else sa.Enum(
            "ADMIN", "USER", "SUPER_ADMIN", name="userrole"
        )

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
                "approval_status", approval_status_col,
                nullable=False, server_default=sa.text("'PENDING'"),
            ),
            sa.Column(
                "role", role_col,
                nullable=False, server_default=sa.text("'USER'"),
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
        op.create_index(op.f("ix_users_id"), "users", ["id"], unique=False)
        op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
        # refresh inspector after creating the table
        inspector = inspect(bind)


    # --- Enums (Postgres) ---
    if bind.dialect.name == "postgresql":
        researcher_status_enum = postgresql.ENUM(
            "PENDING",
            "APPROVED",
            "REJECTED",
            name="researcherrequeststatus",
        )
        researcher_status_enum.create(bind, checkfirst=True)

        cluster_status_enum = postgresql.ENUM(
            "APPROVED",
            "REJECTED",
            "PENDING",
            name="status",
        )
        cluster_status_enum.create(bind, checkfirst=True)

    # --- researcher_requests ---
    if _has_table(inspector, "researcher_requests"):
        if not _has_column(inspector, "researcher_requests", "user_id"):
            op.add_column("researcher_requests", sa.Column("user_id", sa.Integer(), nullable=True))
        if not _has_column(inspector, "researcher_requests", "cluster_id"):
            op.add_column("researcher_requests", sa.Column("cluster_id", sa.Integer(), nullable=True))
        if not _has_column(inspector, "researcher_requests", "status"):
            if bind.dialect.name == "postgresql":
                status_type = postgresql.ENUM(
                    "PENDING",
                    "APPROVED",
                    "REJECTED",
                    name="researcherrequeststatus",
                    create_type=False,
                )
            else:
                status_type = sa.Enum("PENDING", "APPROVED", "REJECTED", name="researcherrequeststatus")
            op.add_column(
                "researcher_requests",
                sa.Column("status", status_type, nullable=True, server_default=sa.text("'PENDING'")),
            )
            op.alter_column("researcher_requests", "status", server_default=None)

        # refresh inspector after DDL changes above
        inspector = inspect(bind)

        # Best-effort data migration
        if bind.dialect.name == "postgresql" and _has_column(inspector, "researcher_requests", "approval_status") and _has_column(
            inspector, "researcher_requests", "status"
        ):
            op.execute(
                sa.text(
                    "UPDATE researcher_requests SET status = approval_status::text::researcherrequeststatus "
                    "WHERE status IS NULL AND approval_status IS NOT NULL"
                )
            )
        if _has_column(inspector, "researcher_requests", "email") and _has_column(
            inspector, "researcher_requests", "user_id"
        ):
            op.execute(
                sa.text(
                    "UPDATE researcher_requests rr "
                    "SET user_id = u.id "
                    "FROM users u "
                    "WHERE rr.user_id IS NULL AND rr.email = u.email"
                )
            )

        if _has_column(inspector, "researcher_requests", "user_id") and not _has_fk_on_column(
            inspector, "researcher_requests", "user_id"
        ):
            op.create_foreign_key(
                "fk_researcher_requests_user_id_users",
                "researcher_requests",
                "users",
                ["user_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if _has_column(inspector, "researcher_requests", "cluster_id") and not _has_fk_on_column(
            inspector, "researcher_requests", "cluster_id"
        ):
            op.create_foreign_key(
                "fk_researcher_requests_cluster_id_device_clusters",
                "researcher_requests",
                "device_clusters",
                ["cluster_id"],
                ["id"],
                ondelete="SET NULL",
            )

        if _has_column(inspector, "researcher_requests", "user_id") and not _has_unique_on_column(
            inspector, "researcher_requests", "user_id"
        ):
            op.create_unique_constraint("uq_researcher_requests_user_id", "researcher_requests", ["user_id"])
        if _has_column(inspector, "researcher_requests", "cluster_id") and not _has_unique_on_column(
            inspector, "researcher_requests", "cluster_id"
        ):
            op.create_unique_constraint("uq_researcher_requests_cluster_id", "researcher_requests", ["cluster_id"])

    # --- device_clusters ---
    if _has_table(inspector, "device_clusters"):
        if not _has_column(inspector, "device_clusters", "cluster_uuid"):
            op.add_column("device_clusters", sa.Column("cluster_uuid", sa.String(length=100), nullable=True))
            inspector = inspect(bind)
            if not _has_index(inspector, "device_clusters", "ix_device_clusters_cluster_uuid"):
                op.create_index("ix_device_clusters_cluster_uuid", "device_clusters", ["cluster_uuid"], unique=True)
        if not _has_column(inspector, "device_clusters", "password"):
            op.add_column("device_clusters", sa.Column("password", sa.String(length=255), nullable=True))
        if not _has_column(inspector, "device_clusters", "public"):
            op.add_column(
                "device_clusters",
                sa.Column("public", sa.Boolean(), nullable=True, server_default=sa.text("false")),
            )
            op.alter_column("device_clusters", "public", server_default=None)
        if not _has_column(inspector, "device_clusters", "status"):
            if bind.dialect.name == "postgresql":
                cluster_status_type = postgresql.ENUM(
                    "APPROVED",
                    "REJECTED",
                    "PENDING",
                    name="status",
                    create_type=False,
                )
            else:
                cluster_status_type = sa.Enum("APPROVED", "REJECTED", "PENDING", name="status")
            op.add_column(
                "device_clusters",
                sa.Column("status", cluster_status_type, nullable=True, server_default=sa.text("'PENDING'")),
            )
            op.alter_column("device_clusters", "status", server_default=None)

    # --- devices ---
    if _has_table(inspector, "devices"):
        if not _has_column(inspector, "devices", "cluster_id"):
            op.add_column("devices", sa.Column("cluster_id", sa.Integer(), nullable=True))
        inspector = inspect(bind)
        if _has_column(inspector, "devices", "cluster_id") and not _has_fk_on_column(inspector, "devices", "cluster_id"):
            op.create_foreign_key(
                "fk_devices_cluster_id_device_clusters",
                "devices",
                "device_clusters",
                ["cluster_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    # Non-destructive sync migration; no downgrade provided.
    pass
