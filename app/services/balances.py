from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import LeaveBalanceLedger
from app.services.policy import LeavePolicy, leave_policy


class BalanceService:
    def __init__(self, db: Session, policy: LeavePolicy = leave_policy):
        self.db = db
        self.policy = policy

    def get_balance(self, employee_id: int, leave_type: str, year: int) -> float:
        total = self.db.scalar(
            select(func.coalesce(func.sum(LeaveBalanceLedger.change_days), 0)).where(
                LeaveBalanceLedger.employee_id == employee_id,
                LeaveBalanceLedger.leave_type == leave_type,
                LeaveBalanceLedger.year == year,
            )
        )
        return float(total or 0)

    def get_taken_days(self, employee_id: int, leave_type: str, year: int) -> float:
        total = self.db.scalar(
            select(func.coalesce(func.sum(LeaveBalanceLedger.change_days), 0)).where(
                LeaveBalanceLedger.employee_id == employee_id,
                LeaveBalanceLedger.leave_type == leave_type,
                LeaveBalanceLedger.year == year,
                LeaveBalanceLedger.reason == "approved_leave",
            )
        )
        return abs(float(total or 0))

    def get_allocated_days(self, employee_id: int, leave_type: str, year: int) -> float:
        total = self.db.scalar(
            select(func.coalesce(func.sum(LeaveBalanceLedger.change_days), 0)).where(
                LeaveBalanceLedger.employee_id == employee_id,
                LeaveBalanceLedger.leave_type == leave_type,
                LeaveBalanceLedger.year == year,
                LeaveBalanceLedger.reason == "annual_allocation",
            )
        )
        return float(total or 0)

    def initialize_default_balances(self, employee_id: int, year: int) -> None:
        for leave_type, rule in self.policy.all().items():
            existing = self.get_balance(employee_id, leave_type, year)
            if existing:
                continue
            self.db.add(
                LeaveBalanceLedger(
                    employee_id=employee_id,
                    leave_type=leave_type,
                    year=year,
                    change_days=rule.annual_days,
                    reason="annual_allocation",
                )
            )

    def initialize_default_balances_for_leave_type(self, leave_type: str, annual_days: float, year: int) -> None:
        from sqlalchemy import select

        from app.db.models import Employee

        for employee in self.db.scalars(select(Employee)).all():
            existing = self.get_allocated_days(employee.id, leave_type, year)
            if existing:
                continue
            self.db.add(
                LeaveBalanceLedger(
                    employee_id=employee.id,
                    leave_type=leave_type,
                    year=year,
                    change_days=annual_days,
                    reason="annual_allocation",
                )
            )

    def deduct_for_request(self, employee_id: int, leave_type: str, year: int, days: float, request_id: int) -> None:
        self.db.add(
            LeaveBalanceLedger(
                employee_id=employee_id,
                leave_type=leave_type,
                year=year,
                change_days=-days,
                reason="approved_leave",
                leave_request_id=request_id,
            )
        )
