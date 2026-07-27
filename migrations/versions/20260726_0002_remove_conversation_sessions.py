"""Remove LLM conversation state.

Revision ID: 20260726_0002
Revises: 20260713_0001
"""
from alembic import op
import sqlalchemy as sa


revision = "20260726_0002"
down_revision = "20260713_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "conversation_sessions" in existing:
        op.drop_table("conversation_sessions")


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "conversation_sessions" not in existing:
        op.create_table(
            "conversation_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("slack_user_id", sa.String(64), nullable=False),
            sa.Column("current_intent", sa.String(64)),
            sa.Column("collected_fields_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_conversation_sessions_slack_user_id", "conversation_sessions", ["slack_user_id"])
