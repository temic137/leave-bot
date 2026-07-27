from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ApprovalEvent, Employee, LeaveRequest, LeaveRequestStatus
from app.schemas.leave import LeaveRequestCreate
from app.services.balances import BalanceService
from app.services.dates import calculate_leave_days
from app.services.policy import LeavePolicy, leave_policy


class LeaveRequestService:
    def __init__(self, db: Session, policy: LeavePolicy = leave_policy):
        self.db = db
        self.policy = policy
        self.balances = BalanceService(db, policy)

    def create_request(self, payload: LeaveRequestCreate) -> LeaveRequest:
        rule = self.policy.get(payload.leave_type)
        employee = self.db.scalar(
            select(Employee).where(Employee.id == payload.employee_id).with_for_update()
        )
        if employee is None:
            raise ValueError("Employee does not exist")
        if payload.start_date.year != payload.end_date.year:
            raise ValueError("A leave request cannot cross into another calendar year. Submit one request per year.")
        days_requested = calculate_leave_days(payload.start_date, payload.end_date)
        if days_requested == 0:
            raise ValueError("The selected dates do not contain any working days.")

        if rule.requires_document and not payload.document_key:
            raise ValueError("This leave type requires a document")
        overlap = self.db.scalar(
            select(LeaveRequest.id).where(
                LeaveRequest.employee_id == payload.employee_id,
                LeaveRequest.status.in_(
                    [
                        LeaveRequestStatus.draft.value,
                        LeaveRequestStatus.pending_manager.value,
                        LeaveRequestStatus.pending_hr.value,
                        LeaveRequestStatus.approved.value,
                    ]
                ),
                LeaveRequest.start_date <= payload.end_date,
                LeaveRequest.end_date >= payload.start_date,
            )
        )
        if overlap:
            raise ValueError("These dates overlap an existing pending or approved leave request.")
        committed = self.balances.get_committed_days(
            payload.employee_id,
            payload.leave_type,
            payload.start_date.year,
        )
        allocated = self.balances.get_allocated_days(
            payload.employee_id,
            payload.leave_type,
            payload.start_date.year,
        )
        remaining = allocated - committed
        if days_requested > remaining:
            raise ValueError(
                f"This request needs {days_requested:g} working days, but only "
                f"{max(remaining, 0):g} days remain for {rule.display_name}."
            )

        request = LeaveRequest(
            employee_id=payload.employee_id,
            leave_type=payload.leave_type,
            start_date=payload.start_date,
            end_date=payload.end_date,
            days_requested=days_requested,
            reason=payload.reason,
            document_key=payload.document_key,
            status=LeaveRequestStatus.pending_manager.value,
        )
        self.db.add(request)
        self.db.flush()
        return request

    def record_manager_decision(self, approver: Employee, request: LeaveRequest, approved: bool, comment: str | None = None) -> LeaveRequest:
        self._record_decision(approver, request, "manager", approved, comment)
        if not approved:
            request.status = LeaveRequestStatus.rejected.value
            request.decided_at = datetime.now(UTC)
            return request

        rule = self.policy.get(request.leave_type)
        if rule.requires_hr:
            request.status = LeaveRequestStatus.pending_hr.value
        else:
            self._approve_and_deduct(request)
        return request

    def record_hr_decision(self, approver: Employee, request: LeaveRequest, approved: bool, comment: str | None = None) -> LeaveRequest:
        self._record_decision(approver, request, "hr", approved, comment)
        if approved:
            self._approve_and_deduct(request)
        else:
            request.status = LeaveRequestStatus.rejected.value
            request.decided_at = datetime.now(UTC)
        return request

    def request_cancellation(self, employee: Employee, request: LeaveRequest) -> bool:
        if request.employee_id != employee.id:
            raise ValueError("You can only cancel your own leave request.")
        if request.status in {
            LeaveRequestStatus.draft.value,
            LeaveRequestStatus.pending_manager.value,
            LeaveRequestStatus.pending_hr.value,
        }:
            request.status = LeaveRequestStatus.cancelled.value
            request.decided_at = datetime.now(UTC)
            self._record_event(employee, request, "employee", "cancelled", "Cancelled by employee")
            return False
        if request.status == LeaveRequestStatus.approved.value:
            request.status = LeaveRequestStatus.pending_cancellation_manager.value
            request.decided_at = None
            self._record_event(
                employee,
                request,
                "employee",
                "cancellation_requested",
                "Cancellation requested by employee",
            )
            return True
        raise ValueError(f"This request is already {request.status.replace('_', ' ')}.")

    def record_cancellation_decision(
        self,
        approver: Employee,
        request: LeaveRequest,
        approved: bool,
    ) -> LeaveRequest:
        if request.status != LeaveRequestStatus.pending_cancellation_manager.value:
            raise ValueError("This request is not waiting for a cancellation decision.")
        if request.employee.manager_id != approver.id and approver.role != "admin":
            raise ValueError("You are not allowed to decide this cancellation.")
        request.status = (
            LeaveRequestStatus.cancelled.value
            if approved
            else LeaveRequestStatus.approved.value
        )
        request.decided_at = datetime.now(UTC)
        self._record_event(
            approver,
            request,
            approver.role,
            "cancellation_approved" if approved else "cancellation_rejected",
            "Slack cancellation decision",
        )
        return request

    def override_request(
        self,
        approver: Employee,
        request: LeaveRequest,
        status: str,
        reason: str,
    ) -> LeaveRequest:
        if (
            approver.role not in {"hr", "admin"}
            or approver.workspace_id != request.employee.workspace_id
        ):
            raise ValueError("You are not allowed to override this request.")
        if status not in {
            LeaveRequestStatus.approved.value,
            LeaveRequestStatus.rejected.value,
            LeaveRequestStatus.cancelled.value,
        }:
            raise ValueError("Choose approved, rejected, or cancelled.")
        if not reason.strip():
            raise ValueError("An override reason is required.")
        request.status = status
        request.decided_at = datetime.now(UTC)
        self._record_event(
            approver,
            request,
            approver.role,
            f"override_{status}",
            reason.strip(),
        )
        return request

    def _record_decision(self, approver: Employee, request: LeaveRequest, role: str, approved: bool, comment: str | None) -> None:
        self._record_event(
            approver,
            request,
            role,
            "approved" if approved else "rejected",
            comment,
        )

    def _record_event(
        self,
        approver: Employee,
        request: LeaveRequest,
        role: str,
        decision: str,
        comment: str | None,
    ) -> None:
        self.db.add(
            ApprovalEvent(
                leave_request_id=request.id,
                approver_id=approver.id,
                approver_role=role,
                decision=decision,
                comment=comment,
            )
        )

    def _approve_and_deduct(self, request: LeaveRequest) -> None:
        request.status = LeaveRequestStatus.approved.value
        request.decided_at = datetime.now(UTC)
        self.balances.deduct_for_request(
            employee_id=request.employee_id,
            leave_type=request.leave_type,
            year=request.start_date.year,
            days=float(request.days_requested),
            request_id=request.id,
        )
