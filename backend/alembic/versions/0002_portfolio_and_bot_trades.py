"""add portfolio snapshots and bot trades

Revision ID: 0002_portfolio_bot_trades
Revises: 0001_initial
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_portfolio_bot_trades"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_trades",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("market_title", sa.String(length=512), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("price", sa.Numeric(18, 8), nullable=False),
        sa.Column("size", sa.Numeric(18, 8), nullable=False),
        sa.Column("notional", sa.Numeric(18, 8), nullable=False),
        sa.Column("pnl_abs", sa.Numeric(18, 8), nullable=False),
        sa.Column("pnl_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bot_trades_market_id", "bot_trades", ["market_id"])
    op.create_index("ix_bot_trades_market_title", "bot_trades", ["market_title"])
    op.create_index("ix_bot_trades_status", "bot_trades", ["status"])
    op.create_index("ix_bot_trades_executed_at", "bot_trades", ["executed_at"])
    op.create_index("ix_bot_trades_market_time", "bot_trades", ["market_id", "executed_at"])

    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("total_equity", sa.Numeric(18, 8), nullable=False),
        sa.Column("capital_in_trade", sa.Numeric(18, 8), nullable=False),
        sa.Column("pnl_abs", sa.Numeric(18, 8), nullable=False),
        sa.Column("pnl_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_portfolio_snapshots_observed_at", "portfolio_snapshots", ["observed_at"])
    op.create_index("ix_portfolio_snapshots_time", "portfolio_snapshots", ["observed_at"])


def downgrade() -> None:
    op.drop_table("portfolio_snapshots")
    op.drop_table("bot_trades")
