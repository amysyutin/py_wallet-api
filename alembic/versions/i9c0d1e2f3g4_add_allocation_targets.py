"""persist user allocation targets

Revision ID: i9c0d1e2f3g4
Revises: h8b9c0d1e2f3
Create Date: 2026-09-04 09:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i9c0d1e2f3g4"
down_revision: Union[str, None] = "h8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "allocation_targets",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("asset_key", sa.String(length=200), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("target_pct", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_pct > 0 AND target_pct <= 100",
            name="ck_allocation_targets_pct_range",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "asset_key",
            name="uq_allocation_targets_user_asset",
        ),
    )
    op.create_index(
        "ix_allocation_targets_user_id",
        "allocation_targets",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_allocation_targets_user_id", table_name="allocation_targets")
    op.drop_table("allocation_targets")
