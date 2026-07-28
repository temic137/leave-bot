"""Add Performance API identifiers.

Revision ID: 20260728_0006
Revises: 20260727_0005
"""
from alembic import op
import sqlalchemy as sa


revision = "20260728_0006"
down_revision = "20260727_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("external_employee_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "employees",
        sa.Column("country", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_employees_external_employee_id",
        "employees",
        ["external_employee_id"],
    )
    op.add_column(
        "leave_requests",
        sa.Column("external_request_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "leave_requests",
        sa.Column("external_leave_type", sa.String(128), nullable=True),
    )
    op.create_index(
        "ix_leave_requests_external_request_id",
        "leave_requests",
        ["external_request_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_leave_requests_external_request_id",
        table_name="leave_requests",
    )
    op.drop_column("leave_requests", "external_leave_type")
    op.drop_column("leave_requests", "external_request_id")
    op.drop_index(
        "ix_employees_external_employee_id",
        table_name="employees",
    )
    op.drop_column("employees", "country")
    op.drop_column("employees", "external_employee_id")
