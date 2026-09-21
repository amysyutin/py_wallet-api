"""enforce active Solana wallet uniqueness

Revision ID: i9c0d1e2f3g4
Revises: h8b9c0d1e2f3
Create Date: 2026-08-28 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i9c0d1e2f3g4"
down_revision: Union[str, None] = "h8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Preserve the oldest active row if Solana wallets were inserted before
    # the API started enforcing the invariant. Base58 addresses are
    # case-sensitive, so only surrounding whitespace is normalized here.
    op.execute(sa.text("""
            WITH ranked AS (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY user_id, btrim(address)
                        ORDER BY id
                    ) AS duplicate_rank
                FROM wallets
                WHERE wallet_type = 'solana'
                  AND is_active IS TRUE
                  AND address IS NOT NULL
            )
            UPDATE wallets
            SET is_active = false
            WHERE id IN (
                SELECT id FROM ranked WHERE duplicate_rank > 1
            )
            """))
    op.create_index(
        "uq_wallets_active_solana_address",
        "wallets",
        ["user_id", sa.text("btrim(address)")],
        unique=True,
        postgresql_where=sa.text(
            "wallet_type = 'solana' AND is_active IS TRUE AND address IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_wallets_active_solana_address", table_name="wallets")
