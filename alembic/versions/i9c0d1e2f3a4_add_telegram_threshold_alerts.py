"""add Telegram portfolio threshold alerts

Revision ID: i9c0d1e2f3a4
Revises: h8b9c0d1e2f3
Create Date: 2026-09-11 09:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i9c0d1e2f3a4"
down_revision: Union[str, None] = "h8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telegram_notification_settings",
        sa.Column("alert_threshold_percent", sa.Numeric(7, 2), nullable=True),
    )
    op.create_check_constraint(
        "ck_telegram_alert_threshold_percent",
        "telegram_notification_settings",
        "alert_threshold_percent IS NULL OR "
        "(alert_threshold_percent >= 0.1 AND alert_threshold_percent <= 1000)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_telegram_alert_threshold_percent",
        "telegram_notification_settings",
        type_="check",
    )
    op.drop_column("telegram_notification_settings", "alert_threshold_percent")
