"""Scope employees and HR access to a Slack workspace.

Revision ID: 20260727_0003
Revises: 20260726_0002
"""
from alembic import op
import sqlalchemy as sa


revision = "20260727_0003"
down_revision = "20260726_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("workspace_id", sa.String(64), nullable=True))
    op.create_index("ix_employees_workspace_id", "employees", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_employees_workspace_id", table_name="employees")
    op.drop_column("employees", "workspace_id")
