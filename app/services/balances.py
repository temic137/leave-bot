from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.performance import PerformanceAPIClient
from app.core.config import settings
from app.db.models import Employee, LeaveBalanceAdjustment, LeaveRequest, LeaveRequestStatus
from app.services.dates import calculate_leave_days
from app.services.policy import LeavePolicy, leave_policy


class BalanceService:
    def __init__(
        self,
        db: Session,
        policy: LeavePolicy = leave_policy,
        external: PerformanceAPIClient | None = None,
    ):
        self.db = db
        self.policy = policy
        self.external = external or PerformanceAPIClient()
        self.live = settings.performance_api_mode.lower() == "live"

    def get_balance(self, employee_id: int, leave_type: str, year: int) -> float:
        return self.get_remaining_days(employee_id, leave_type, year)

    def get_taken_days(self, employee_id: int, leave_type: str, year: int) -> float:
        if self.live:
            employee = self._employee(employee_id)
            rule = self.policy.get(leave_type)
            return sum(
                self._external_request_days(row, rule.count_weekends, year)
                for row in self.external.list_requests(employee.email)
                if str(row.get("status") or "").lower() == "approved"
                and PerformanceAPIClient.matches_leave_type(
                    str(row.get("leavetype") or ""),
                    leave_type,
                    rule.display_name,
                )
            )
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
        if self.live:
            employee = self._employee(employee_id)
            rule = self.policy.get(leave_type)
            balance = self.external.find_balance(
                employee.email,
                leave_type,
                rule.display_name,
            )
            if balance is None:
                raise ValueError(
                    f"{employee.name} is not eligible for {rule.display_name}."
                )
            return float(balance.get("balance") or 0) + self.get_taken_days(
                employee_id,
                leave_type,
                year,
            )
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
        if self.live:
            employees = {
                employee.id: employee
                for employee in self.db.scalars(
                    select(Employee).where(Employee.id.in_(employee_ids))
                )
            }
            requests = self.external.list_requests()
            result = {employee_id: {} for employee_id in employee_ids}
            for row in requests:
                if str(row.get("status") or "").lower() != "approved":
                    continue
                employee = next(
                    (
                        item
                        for item in employees.values()
                        if item.email.lower() == str(row.get("email") or "").lower()
                    ),
                    None,
                )
                policy_key = self._policy_key(str(row.get("leavetype") or ""))
                if employee is None or policy_key is None:
                    continue
                rule = self.policy.get(policy_key)
                days = self._external_request_days(row, rule.count_weekends, year)
                result[employee.id][policy_key] = (
                    result[employee.id].get(policy_key, 0.0) + days
                )
            return result
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
                leave_type: 0.0 if self.live else rule.annual_days
                for leave_type, rule in self.policy.all().items()
            }
            for employee_id in employee_ids
        }
        if not employee_ids:
            return allocations
        if self.live:
            employees = {
                employee.email.lower(): employee
                for employee in self.db.scalars(
                    select(Employee).where(Employee.id.in_(employee_ids))
                )
            }
            used = self.get_taken_days_for_employees(employee_ids, year)
            for row in self.external.list_balances():
                employee = employees.get(str(row.get("email") or "").lower())
                policy_key = self._policy_key(str(row.get("leavetype") or ""))
                if employee is not None and policy_key is not None:
                    allocations[employee.id][policy_key] = float(
                        row.get("balance") or 0
                    ) + used.get(employee.id, {}).get(policy_key, 0.0)
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

    def get_eligible_leave_types_for_employees(
        self,
        employee_ids: list[int],
    ) -> dict[int, set[str]]:
        if not self.live:
            return {
                employee_id: set(self.policy.all())
                for employee_id in employee_ids
            }
        employees = {
            employee.email.lower(): employee
            for employee in self.db.scalars(
                select(Employee).where(Employee.id.in_(employee_ids))
            )
        }
        eligible = {employee_id: set() for employee_id in employee_ids}
        for row in self.external.list_balances():
            employee = employees.get(str(row.get("email") or "").lower())
            policy_key = self._policy_key(str(row.get("leavetype") or ""))
            if employee is not None and policy_key is not None:
                eligible[employee.id].add(policy_key)
        return eligible

    def get_committed_days(self, employee_id: int, leave_type: str, year: int) -> float:
        if self.live:
            status_condition = LeaveRequest.status.in_(
                [
                    LeaveRequestStatus.draft.value,
                    LeaveRequestStatus.pending_manager.value,
                    LeaveRequestStatus.pending_hr.value,
                ]
            ) | (
                (
                    LeaveRequest.status == LeaveRequestStatus.approved.value
                )
                & LeaveRequest.external_request_id.is_(None)
            )
        else:
            status_condition = LeaveRequest.status.in_(
                [
                    LeaveRequestStatus.draft.value,
                    LeaveRequestStatus.pending_manager.value,
                    LeaveRequestStatus.pending_hr.value,
                    LeaveRequestStatus.approved.value,
                    LeaveRequestStatus.pending_cancellation_manager.value,
                ]
            )
        total = self.db.scalar(
            select(func.coalesce(func.sum(LeaveRequest.days_requested), 0)).where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.leave_type == leave_type,
                status_condition,
                func.extract("year", LeaveRequest.start_date) == year,
            )
        )
        external_used = (
            self.get_taken_days(employee_id, leave_type, year)
            if self.live
            else 0.0
        )
        return external_used + float(total or 0)

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
        return self.record_adjustment(
            adjuster,
            employee,
            leave_type,
            year,
            days_delta,
            reason,
        )

    def record_adjustment(
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

    def _employee(self, employee_id: int) -> Employee:
        employee = self.db.get(Employee, employee_id)
        if employee is None:
            raise ValueError("Employee does not exist")
        return employee

    def _policy_key(self, external_leave_type: str) -> str | None:
        return next(
            (
                key
                for key, rule in self.policy.all().items()
                if PerformanceAPIClient.matches_leave_type(
                    external_leave_type,
                    key,
                    rule.display_name,
                )
            ),
            None,
        )

    @staticmethod
    def _external_request_days(
        row: dict,
        count_weekends: bool,
        year: int,
    ) -> float:
        try:
            start = date.fromisoformat(str(row.get("startdate") or "")[:10])
            end = date.fromisoformat(str(row.get("enddate") or "")[:10])
        except ValueError:
            return 0.0
        if start.year != year:
            return 0.0
        return calculate_leave_days(start, end, count_weekends)
