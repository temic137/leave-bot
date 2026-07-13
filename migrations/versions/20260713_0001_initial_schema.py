"""Create the leave bot schema and durable job queue.

Revision ID: 20260713_0001
Revises:
"""
from alembic import op
import sqlalchemy as sa


revision = "20260713_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "employees" not in existing:
        op.create_table(
            "employees",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("slack_user_id", sa.String(64), nullable=False),
            sa.Column("email", sa.String(255), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("role", sa.String(32), nullable=False),
            sa.Column("department", sa.String(128)),
            sa.Column("manager_id", sa.Integer(), sa.ForeignKey("employees.id")),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("slack_user_id"),
            sa.UniqueConstraint("email"),
        )
        op.create_index("ix_employees_slack_user_id", "employees", ["slack_user_id"], unique=True)
        op.create_index("ix_employees_email", "employees", ["email"], unique=True)

    if "leave_requests" not in existing:
        op.create_table(
            "leave_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
            sa.Column("leave_type", sa.String(64), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("days_requested", sa.Numeric(6, 2), nullable=False),
            sa.Column("reason", sa.Text()),
            sa.Column("document_key", sa.String(512)),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("agentspan_execution_id", sa.String(255)),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("decided_at", sa.DateTime()),
        )
        op.create_index("ix_leave_requests_employee_id", "leave_requests", ["employee_id"])
        op.create_index("ix_leave_requests_leave_type", "leave_requests", ["leave_type"])
        op.create_index("ix_leave_requests_agentspan_execution_id", "leave_requests", ["agentspan_execution_id"])

    if "approval_events" not in existing:
        op.create_table(
            "approval_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("leave_request_id", sa.Integer(), sa.ForeignKey("leave_requests.id"), nullable=False),
            sa.Column("approver_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
            sa.Column("approver_role", sa.String(32), nullable=False),
            sa.Column("decision", sa.String(32), nullable=False),
            sa.Column("comment", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_approval_events_leave_request_id", "approval_events", ["leave_request_id"])
        op.create_index("ix_approval_events_approver_id", "approval_events", ["approver_id"])

    if "leave_policy_versions" not in existing:
        op.create_table(
            "leave_policy_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("raw_text", sa.Text(), nullable=False),
            sa.Column("rules_json", sa.Text(), nullable=False),
            sa.Column("created_by", sa.String(255), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("version"),
        )
        op.create_index("ix_leave_policy_versions_version", "leave_policy_versions", ["version"], unique=True)

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

    if "durable_jobs" not in existing:
        op.create_table(
            "durable_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_type", sa.String(64), nullable=False),
            sa.Column("idempotency_key", sa.String(255), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("max_attempts", sa.Integer(), nullable=False),
            sa.Column("available_at", sa.DateTime(), nullable=False),
            sa.Column("locked_at", sa.DateTime()),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("idempotency_key"),
        )
        op.create_index("ix_durable_jobs_job_type", "durable_jobs", ["job_type"])
        op.create_index("ix_durable_jobs_idempotency_key", "durable_jobs", ["idempotency_key"], unique=True)
        op.create_index("ix_durable_jobs_status", "durable_jobs", ["status"])
        op.create_index("ix_durable_jobs_available_at", "durable_jobs", ["available_at"])


def downgrade() -> None:
    op.drop_table("durable_jobs")
