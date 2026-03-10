"""add_wallet_version

Revision ID: 7b2f4d1a9c0e
Revises: 1d1b8bc5bb73
Create Date: 2026-03-10

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7b2f4d1a9c0e"
down_revision: Union[str, Sequence[str], None] = "1d1b8bc5bb73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wallets",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("wallets", "version")

