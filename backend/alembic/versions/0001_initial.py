"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("slug", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    status_enum = sa.Enum("active", "closed", "resolved", name="marketstatus")
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "markets",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("event_id", sa.String(length=128), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("condition_id", sa.String(length=128), nullable=True),
        sa.Column("slug", sa.String(length=255), nullable=True),
        sa.Column("question", sa.String(length=512), nullable=False),
        sa.Column("outcomes", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("status", status_enum, nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "tokens",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("market_id", sa.String(length=128), sa.ForeignKey("markets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
    )

    op.create_table(
        "raw_metadata_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "raw_websocket_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "trades",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("market_id", sa.String(length=128), sa.ForeignKey("markets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("price", sa.Numeric(18, 8), nullable=False),
        sa.Column("size", sa.Numeric(18, 8), nullable=False),
        sa.Column("traded_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "top_of_book",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("market_id", sa.String(length=128), sa.ForeignKey("markets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_id", sa.String(length=128), nullable=False),
        sa.Column("best_bid", sa.Numeric(18, 8), nullable=True),
        sa.Column("best_ask", sa.Numeric(18, 8), nullable=True),
        sa.Column("mid_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("spread", sa.Numeric(18, 8), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "sync_job_status",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("sync_job_status")
    op.drop_table("top_of_book")
    op.drop_table("trades")
    op.drop_table("raw_websocket_messages")
    op.drop_table("raw_metadata_snapshots")
    op.drop_table("tokens")
    op.drop_table("markets")
    op.drop_table("events")
