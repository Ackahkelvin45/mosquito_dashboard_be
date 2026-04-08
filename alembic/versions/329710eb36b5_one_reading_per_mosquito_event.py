"""one reading per mosquito event

Revision ID: 329710eb36b5
Revises: 3bcadda4a0f6
Create Date: 2026-04-08

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "329710eb36b5"
down_revision: Union[str, Sequence[str], None] = "3bcadda4a0f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_unique_on_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    for uc in inspector.get_unique_constraints(table_name):
        if uc.get("column_names") == [column_name]:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not _has_table(inspector, "mosquito_individual_readings"):
        return

    if _has_unique_on_column(inspector, "mosquito_individual_readings", "batch_id"):
        return

    # Refuse to apply if duplicates exist.
    duplicates = bind.execute(
        sa.text(
            "SELECT batch_id FROM mosquito_individual_readings "
            "GROUP BY batch_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicates:
        raise RuntimeError(
            f"Cannot enforce one-reading-per-event: duplicates found for batch_id={duplicates[0]}"
        )

    op.create_unique_constraint(
        "uq_mosquito_individual_readings_batch_id",
        "mosquito_individual_readings",
        ["batch_id"],
    )


def downgrade() -> None:
    # Non-destructive constraint add; no downgrade.
    pass
