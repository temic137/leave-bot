from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Employee, LeaveBalanceAdjustment, LeaveRequest, LeaveRequestStatus
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
                LeaveRequest.status.in_(
                    [
                        LeaveRequestStatus.approved.value,
                        LeaveRequestStatus.pending_cancellation_manager.value,
                    ]
                ),
                func.extract("year", LeaveRequest.start_date) == year,
            )
        )
        return float(total or 0)

    def get_allocated_days(self, employee_id: int, leave_type: str, year: int) -> float:
        adjustment = self.db.scalar(
            select(func.coalesce(func.sum(LeaveBalanceAdjustment.days_delta), 0)).where(
                LeaveBalanceAdjustment.employee_id == employee_id,
                LeaveBalanceAdjustment.leave_type == leave_type,
                LeaveBalanceAdjustment.year == year,
            )
        )
        return self.policy.get(leave_type).annual_days + float(adjustment or 0)

    def get_remaining_days(self, employee_id: int, leave_type: str, year: int) -> float:
        return self.get_allocated_days(employee_id, leave_type, year) - self.get_taken_days(
            employee_id, leave_type, year
        )

    def get_taken_days_for_employees(
        self,
        employee_ids: list[int],
        year: int,
        statuses: tuple[str, ...] = (
            LeaveRequestStatus.approved.value,
            LeaveRequestStatus.pending_cancellation_manager.value,
        ),
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

    def get_allocated_days_for_employees(
        self,
        employee_ids: list[int],
        year: int,
    ) -> dict[int, dict[str, float]]:
        allocations = {
            employee_id: {
                leave_type: rule.annual_days
                for leave_type, rule in self.policy.all().items()
            }
            for employee_id in employee_ids
        }
        if not employee_ids:
            return allocations
        rows = self.db.execute(
            select(
                LeaveBalanceAdjustment.employee_id,
                LeaveBalanceAdjustment.leave_type,
                func.sum(LeaveBalanceAdjustment.days_delta),
            )
            .where(
                LeaveBalanceAdjustment.employee_id.in_(employee_ids),
                LeaveBalanceAdjustment.year == year,
            )
            .group_by(
                LeaveBalanceAdjustment.employee_id,
                LeaveBalanceAdjustment.leave_type,
            )
        ).all()
        for employee_id, leave_type, adjustment in rows:
            if leave_type in allocations[employee_id]:
                allocations[employee_id][leave_type] += float(adjustment)
        return allocations

    def get_committed_days(self, employee_id: int, leave_type: str, year: int) -> float:
        balances = self.get_taken_days_for_employees(
            [employee_id],
            year,
            (
                LeaveRequestStatus.draft.value,
                LeaveRequestStatus.pending_manager.value,
                LeaveRequestStatus.pending_hr.value,
                LeaveRequestStatus.approved.value,
                LeaveRequestStatus.pending_cancellation_manager.value,
            ),
        )
        return balances.get(employee_id, {}).get(leave_type, 0.0)

    def adjust_allocation(
        self,
        adjuster: Employee,
        employee: Employee,
        leave_type: str,
        year: int,
        days_delta: float,
        reason: str,
    ) -> LeaveBalanceAdjustment:
        if adjuster.role not in {"hr", "admin"} or adjuster.workspace_id != employee.workspace_id:
            raise ValueError("You are not allowed to adjust this employee's balance.")
        if leave_type not in self.policy.all():
            raise ValueError("Choose a valid leave type.")
        if not reason.strip():
            raise ValueError("A reason is required.")
        if days_delta == 0:
            raise ValueError("The adjustment cannot be zero.")
        self.db.scalar(select(Employee.id).where(Employee.id == employee.id).with_for_update())
        new_allocation = self.get_allocated_days(employee.id, leave_type, year) + days_delta
        used = self.get_taken_days(employee.id, leave_type, year)
        if new_allocation < used:
            raise ValueError(
                f"The adjusted allocation cannot be lower than {used:g} used days."
            )
        adjustment = LeaveBalanceAdjustment(
            employee_id=employee.id,
            adjusted_by_id=adjuster.id,
            leave_type=leave_type,
            year=year,
            days_delta=days_delta,
            reason=reason.strip(),
        )
        self.db.add(adjustment)
        self.db.flush()
        return adjustment

    def initialize_default_balances(self, employee_id: int, year: int) -> None:
        return None

    def initialize_default_balances_for_leave_type(self, leave_type: str, annual_days: float, year: int) -> None:
        return None

    def deduct_for_request(self, employee_id: int, leave_type: str, year: int, days: float, request_id: int) -> None:
        return None
