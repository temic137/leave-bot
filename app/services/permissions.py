from app.db.models import Employee, LeaveRequest


def can_view_balance(requester: Employee, target: Employee) -> bool:
    if requester.id == target.id:
        return True
    if target.manager_id == requester.id:
        return True
    return requester.role in {"hr", "admin"}


def can_view_document(requester: Employee, leave_request: LeaveRequest) -> bool:
    owner = leave_request.employee
    if requester.id == owner.id:
        return True
    if owner.manager_id == requester.id:
        return True
    return requester.role in {"hr", "admin"}


def can_approve_request(requester: Employee, leave_request: LeaveRequest) -> bool:
    owner = leave_request.employee
    if leave_request.status == "pending_manager":
        return owner.manager_id == requester.id or requester.role == "admin"
    if leave_request.status == "pending_hr":
        return requester.role in {"hr", "admin"}
    return False

