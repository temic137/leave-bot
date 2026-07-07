from datetime import UTC, datetime

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
        days_requested = calculate_leave_days(payload.start_date, payload.end_date)

        if rule.requires_document and not payload.document_key:
            raise ValueError("This leave type requires a document")

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

    def _record_decision(self, approver: Employee, request: LeaveRequest, role: str, approved: bool, comment: str | None) -> None:
        self.db.add(
            ApprovalEvent(
                leave_request_id=request.id,
                approver_id=approver.id,
                approver_role=role,
                decision="approved" if approved else "rejected",
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
