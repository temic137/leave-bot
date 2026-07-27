"""Recalculate request durations as weekdays.

Revision ID: 20260727_0004
Revises: 20260727_0003
"""
from datetime import date

from alembic import op
import sqlalchemy as sa


revision = "20260727_0004"
down_revision = "20260727_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    requests = connection.execute(
        sa.text("SELECT id, start_date, end_date FROM leave_requests")
    ).mappings()
    for request in requests:
        start = _date(request["start_date"])
        end = _date(request["end_date"])
        working_days = sum(
            date.fromordinal(day).weekday() < 5
            for day in range(start.toordinal(), end.toordinal() + 1)
        )
        connection.execute(
            sa.text("UPDATE leave_requests SET days_requested = :days WHERE id = :id"),
            {"days": working_days, "id": request["id"]},
        )


def downgrade() -> None:
    pass


def _date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)
