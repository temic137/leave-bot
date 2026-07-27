from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import LeaveRequest, LeaveRequestStatus
from app.services.policy import LeavePolicy, leave_policy


class BalanceService:
    def __init__(self, db: Session, policy: LeavePolicy = leave_policy):
        self.db = db
        self.policy = policy

    def get_balance(self, employee_id: int, leave_type: str, year: int) -> float:
        return self.get_remaining_days(employee_id, leave_type, year)

    def get_taken_days(self, employee_id: int, leave_type: str, year: int) -> float:
        total = self.db.scalar(
            select(func.coalesce(func.sum(LeaveRequest.days_requested), 0)).where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.leave_type == leave_type,
                LeaveRequest.status == LeaveRequestStatus.approved.value,
                func.extract("year", LeaveRequest.start_date) == year,
            )
        )
        return float(total or 0)

    def get_allocated_days(self, employee_id: int, leave_type: str, year: int) -> float:
        return self.policy.get(leave_type).annual_days

    def get_remaining_days(self, employee_id: int, leave_type: str, year: int) -> float:
        return self.get_allocated_days(employee_id, leave_type, year) - self.get_taken_days(
            employee_id, leave_type, year
        )

    def get_taken_days_for_employees(
        self,
        employee_ids: list[int],
        year: int,
        statuses: tuple[str, ...] = (LeaveRequestStatus.approved.value,),
    ) -> dict[int, dict[str, float]]:
        if not employee_ids:
            return {}
        rows = self.db.execute(
            select(
                LeaveRequest.employee_id,
                LeaveRequest.leave_type,
                func.sum(LeaveRequest.days_requested),
            )
            .where(
                LeaveRequest.employee_id.in_(employee_ids),
                LeaveRequest.status.in_(statuses),
                func.extract("year", LeaveRequest.start_date) == year,
            )
            .group_by(LeaveRequest.employee_id, LeaveRequest.leave_type)
        ).all()
        return {
            employee_id: {
                leave_type: float(total)
                for row_employee_id, leave_type, total in rows
                if row_employee_id == employee_id
            }
            for employee_id in employee_ids
        }

    def get_committed_days(self, employee_id: int, leave_type: str, year: int) -> float:
        balances = self.get_taken_days_for_employees(
            [employee_id],
            year,
            (
                LeaveRequestStatus.draft.value,
                LeaveRequestStatus.pending_manager.value,
                LeaveRequestStatus.pending_hr.value,
                LeaveRequestStatus.approved.value,
            ),
        )
        return balances.get(employee_id, {}).get(leave_type, 0.0)

    def initialize_default_balances(self, employee_id: int, year: int) -> None:
        return None

    def initialize_default_balances_for_leave_type(self, leave_type: str, annual_days: float, year: int) -> None:
        return None

    def deduct_for_request(self, employee_id: int, leave_type: str, year: int, days: float, request_id: int) -> None:
        return None
