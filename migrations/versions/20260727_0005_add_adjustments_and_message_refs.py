"""Add balance adjustments and Slack workflow references.

Revision ID: 20260727_0005
Revises: 20260727_0004
"""
from alembic import op
import sqlalchemy as sa


revision = "20260727_0005"
down_revision = "20260727_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "leave_requests",
        sa.Column("cancellation_agentspan_execution_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "leave_requests",
        sa.Column("slack_message_refs", sa.Text(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_leave_requests_cancellation_agentspan_execution_id",
        "leave_requests",
        ["cancellation_agentspan_execution_id"],
    )
    op.create_table(
        "leave_balance_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("adjusted_by_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("leave_type", sa.String(64), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("days_delta", sa.Numeric(6, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_leave_balance_adjustments_employee_id",
        "leave_balance_adjustments",
        ["employee_id"],
    )
    op.create_index(
        "ix_leave_balance_adjustments_adjusted_by_id",
        "leave_balance_adjustments",
        ["adjusted_by_id"],
    )
    op.create_index(
        "ix_leave_balance_adjustments_leave_type",
        "leave_balance_adjustments",
        ["leave_type"],
    )
    op.create_index(
        "ix_leave_balance_adjustments_year",
        "leave_balance_adjustments",
        ["year"],
    )


def downgrade() -> None:
    op.drop_table("leave_balance_adjustments")
    op.drop_index(
        "ix_leave_requests_cancellation_agentspan_execution_id",
        table_name="leave_requests",
    )
    op.drop_column("leave_requests", "slack_message_refs")
    op.drop_column("leave_requests", "cancellation_agentspan_execution_id")
