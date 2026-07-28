from datetime import date
import hashlib
import hmac
import json
import logging
import re
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.adapters.slack import RealSlackClient
from app.adapters.performance import PerformanceAPIClient, PerformanceAPIError
from app.core.config import settings
from app.db.models import DurableJob, Employee, LeavePolicyVersion, LeaveRequest, LeaveRequestStatus
from app.db.session import get_db
from app.schemas.leave import BalanceRead, LeaveRequestCreate, LeaveRequestRead
from app.services.balances import BalanceService
from app.services.employee_sync import EmployeeSyncService
from app.services.intents import get_intent_router
from app.services.leave_requests import LeaveRequestService
from app.services.jobs import enqueue_job
from app.services.permissions import can_approve_request, can_view_balance
from app.services.policy import leave_policy
from app.services.presentation import leave_name, readable_date, readable_status


router = APIRouter()
logger = logging.getLogger(__name__)


class DecisionIn(BaseModel):
    approver_slack_user_id: str
    approved: bool
    comment: str | None = None


class LeavePolicyIn(BaseModel):
    key: str
    display_name: str
    annual_days: float = 0
    requires_document: bool = False
    requires_hr: bool = False
    allow_negative_balance: bool = False
    min_notice_days: int = 0
    max_request_days: float | None = None
    count_weekends: bool = False


class PolicyTextIn(BaseModel):
    text: str


class ChatIn(BaseModel):
    slack_user_id: str
    text: str
    workspace_id: str | None = None


class EmployeeIn(BaseModel):
    name: str
    email: str
    slack_user_id: str
    role: str = "employee"
    department: str | None = None
    manager_id: int | None = None
    workspace_id: str | None = None
    external_employee_id: str | None = None
    country: str | None = None


@router.post("/admin/init-db")
def init_db() -> dict[str, str]:
    return {"status": "managed by Alembic migrations"}


@router.post("/admin/sync/slack")
def sync_real_slack(db: Session = Depends(get_db)) -> dict[str, int]:
    slack = RealSlackClient()
    service = EmployeeSyncService(db)
    count = 0
    for user in slack.list_users():
        service.upsert_slack_user(
            user.slack_user_id,
            user.email,
            user.name,
            user.is_active,
            user.workspace_id,
        )
        count += 1
    db.commit()
    return {"users_upserted": count}


@router.post("/admin/sync/performance-employees")
def sync_performance_employees(db: Session = Depends(get_db)) -> dict[str, int]:
    client = PerformanceAPIClient()
    service = EmployeeSyncService(db)
    count = 0
    for record in client.list_employees():
        service.upsert_external_employee(record)
        count += 1
    db.commit()
    return {"users_upserted": count}


@router.get("/admin/performance-status")
def performance_status() -> dict:
    client = PerformanceAPIClient()
    result = {
        "mode": settings.performance_api_mode,
        "configured": client.configured,
    }
    if not client.configured:
        return result
    try:
        result.update(
            {
                "reachable": True,
                "employees": len(client.list_employees()),
                "balances": len(client.list_balances()),
                "requests": len(client.list_requests()),
            }
        )
    except PerformanceAPIError as exc:
        result.update({"reachable": False, "error": str(exc)})
    return result


@router.get("/admin/slack-users")
def list_real_slack_users() -> list[dict]:
    return RealSlackClient().list_user_directory()


@router.post("/admin/employees")
def create_employee(payload: EmployeeIn, db: Session = Depends(get_db)) -> dict:
    existing = db.scalar(select(Employee).where(Employee.slack_user_id == payload.slack_user_id))
    if existing is None:
        existing = db.scalar(select(Employee).where(Employee.email == payload.email))
    if existing is None:
        employee = Employee(
            workspace_id=payload.workspace_id,
            external_employee_id=payload.external_employee_id,
            slack_user_id=payload.slack_user_id,
            email=payload.email,
            name=payload.name,
            role=payload.role,
            department=payload.department,
            country=payload.country,
            manager_id=payload.manager_id,
        )
        db.add(employee)
    else:
        employee = existing
        employee.slack_user_id = payload.slack_user_id
        employee.name = payload.name
        employee.role = payload.role
        employee.department = payload.department
        employee.manager_id = payload.manager_id
        employee.external_employee_id = (
            payload.external_employee_id or employee.external_employee_id
        )
        employee.country = payload.country or employee.country
        if payload.workspace_id:
            employee.workspace_id = payload.workspace_id
    db.flush()
    db.commit()
    db.refresh(employee)
    return {
        "id": employee.id,
        "workspace_id": employee.workspace_id,
        "external_employee_id": employee.external_employee_id,
        "slack_user_id": employee.slack_user_id,
        "email": employee.email,
        "name": employee.name,
        "role": employee.role,
        "department": employee.department,
        "country": employee.country,
        "manager_id": employee.manager_id,
    }


@router.get("/admin/leave-types")
def list_leave_types(db: Session = Depends(get_db)) -> dict:
    _sync_policy_from_db(db)
    return {
        key: {
            "key": key,
            "display_name": value.display_name,
            "annual_days": value.annual_days,
            "requires_document": value.requires_document,
            "requires_hr": value.requires_hr,
            "allow_negative_balance": value.allow_negative_balance,
            "min_notice_days": value.min_notice_days,
            "max_request_days": value.max_request_days,
            "count_weekends": value.count_weekends,
        }
        for key, value in leave_policy.all().items()
    }


@router.post("/admin/leave-types")
def upsert_leave_type(payload: LeavePolicyIn, db: Session = Depends(get_db)) -> dict:
    _sync_policy_from_db(db)
    try:
        rule = leave_policy.upsert(
            key=payload.key,
            display_name=payload.display_name,
            annual_days=payload.annual_days,
            requires_document=payload.requires_document,
            requires_hr=payload.requires_hr,
            allow_negative_balance=payload.allow_negative_balance,
            min_notice_days=payload.min_notice_days,
            max_request_days=payload.max_request_days,
            count_weekends=payload.count_weekends,
            persist=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    next_version = (db.scalar(select(func.max(LeavePolicyVersion.version))) or 0) + 1
    db.add(
        LeavePolicyVersion(
            version=next_version,
            raw_text=leave_policy.to_raw_text(),
            rules_json=_policy_rules_json(),
        )
    )
    db.commit()
    return {
        "key": rule.key,
        "display_name": rule.display_name,
        "annual_days": rule.annual_days,
        "requires_document": rule.requires_document,
        "requires_hr": rule.requires_hr,
        "allow_negative_balance": rule.allow_negative_balance,
        "min_notice_days": rule.min_notice_days,
        "max_request_days": rule.max_request_days,
        "count_weekends": rule.count_weekends,
    }


@router.get("/admin/leave-policy-text")
def get_leave_policy_text(db: Session = Depends(get_db)) -> dict:
    version = _sync_policy_from_db(db)
    return {"text": leave_policy.to_raw_text(), "version": version.version}


@router.get("/admin/leave-policy-versions")
def get_leave_policy_versions(db: Session = Depends(get_db)) -> list[dict]:
    _sync_policy_from_db(db)
    versions = db.scalars(select(LeavePolicyVersion).order_by(LeavePolicyVersion.version.desc())).all()
    return [
        {
            "version": item.version,
            "raw_text": item.raw_text,
            "created_by": item.created_by,
            "created_at": item.created_at.isoformat(),
        }
        for item in versions
    ]


@router.put("/admin/leave-policy-text")
def update_leave_policy_text(payload: PolicyTextIn, db: Session = Depends(get_db)) -> dict:
    _sync_policy_from_db(db)
    try:
        rules = leave_policy.load_raw_text(payload.text)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    next_version = (db.scalar(select(func.max(LeavePolicyVersion.version))) or 0) + 1
    db.add(
        LeavePolicyVersion(
            version=next_version,
            raw_text=payload.text.strip() + "\n",
            rules_json=_policy_rules_json(),
        )
    )
    db.commit()
    return {"status": "saved", "version": next_version, "leave_types": list(rules.keys())}


@router.get("/admin/state")
def admin_state(db: Session = Depends(get_db)) -> dict:
    employees = db.scalars(select(Employee).order_by(Employee.id)).all()
    requests = db.scalars(select(LeaveRequest).order_by(LeaveRequest.id.desc())).all()
    target_year = date.today().year
    balance_service = BalanceService(db)
    grouped_balances = balance_service.get_taken_days_for_employees(
        [employee.id for employee in employees],
        target_year,
    )
    grouped_allocations = balance_service.get_allocated_days_for_employees(
        [employee.id for employee in employees],
        target_year,
    )

    return {
        "employees": [
            {
                "id": employee.id,
                "workspace_id": employee.workspace_id,
                "external_employee_id": employee.external_employee_id,
                "slack_user_id": employee.slack_user_id,
                "name": employee.name,
                "email": employee.email,
                "role": employee.role,
                "department": employee.department,
                "country": employee.country,
                "manager_id": employee.manager_id,
                "manager_name": employee.manager.name if employee.manager else None,
                "balances": {
                    leave_type: {
                        "allocated": grouped_allocations[employee.id][leave_type],
                        "used": grouped_balances.get(employee.id, {}).get(leave_type, 0.0),
                        "remaining": (
                            grouped_allocations[employee.id][leave_type]
                            - grouped_balances.get(employee.id, {}).get(leave_type, 0.0)
                        ),
                    }
                    for leave_type in leave_policy.all()
                },
            }
            for employee in employees
        ],
        "requests": [
            {
                "id": request.id,
                "external_request_id": request.external_request_id,
                "external_leave_type": request.external_leave_type,
                "employee_id": request.employee_id,
                "employee_name": request.employee.name,
                "leave_type": request.leave_type,
                "start_date": str(request.start_date),
                "end_date": str(request.end_date),
                "days_requested": float(request.days_requested),
                "reason": request.reason,
                "document_key": request.document_key,
                "status": request.status,
                "agentspan_execution_id": request.agentspan_execution_id,
            }
            for request in requests
        ],
        "leave_types": {
            key: {
                "display_name": value.display_name,
                "annual_days": value.annual_days,
                "requires_document": value.requires_document,
                "requires_hr": value.requires_hr,
                "allow_negative_balance": value.allow_negative_balance,
                "min_notice_days": value.min_notice_days,
                "max_request_days": value.max_request_days,
                "count_weekends": value.count_weekends,
            }
            for key, value in leave_policy.all().items()
        },
    }


@router.post("/slack/events")
async def slack_events(
    request: Request,
    x_slack_signature: str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_retry_num: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    raw_body = await request.body()
    _verify_slack_signature(raw_body, x_slack_signature, x_slack_request_timestamp)
    payload = json.loads(raw_body.decode("utf-8"))

    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}

    if payload.get("type") == "event_callback":
        event_id = payload.get("event_id") or hashlib.sha256(raw_body).hexdigest()
        enqueue_job(
            db,
            "process_slack_event",
            f"slack-event:{event_id}",
            {"slack_payload": payload, "retry_num": x_slack_retry_num},
        )
        db.commit()
        logger.info(
            "Slack event accepted",
            extra={
                "slack_event_id": event_id,
                "slack_user_id": payload.get("event", {}).get("user"),
            },
        )
    return {"ok": True}


@router.post("/slack/commands")
async def slack_commands(
    request: Request,
    x_slack_signature: str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    raw_body = await request.body()
    _verify_slack_signature(raw_body, x_slack_signature, x_slack_request_timestamp)
    form = {key: values[0] for key, values in parse_qs(raw_body.decode("utf-8")).items()}
    command = form.get("command", "")
    user_id = form.get("user_id", "")
    employee = _employee_by_slack(db, user_id, form.get("team_id"))
    if employee is None:
        return _ephemeral("Your Slack account is not registered in the leave system.")

    _sync_policy_from_db(db)
    if command == "/leave-request":
        trigger_id = form.get("trigger_id")
        if not trigger_id:
            return _ephemeral("Slack did not provide a trigger ID. Please run the command again.")
        RealSlackClient().open_leave_request_modal(trigger_id, leave_policy.all())
        return {"response_type": "ephemeral", "text": ""}

    if command == "/leave-balance":
        return _ephemeral(_balance_result(db, employee)["reply"])

    if command == "/leave-history":
        return _ephemeral(_history_result(db, employee)["reply"])

    if command == "/leave-admin":
        return _ephemeral(_pending_requests_result(db, employee)["reply"])

    if command == "/leave-set-manager":
        manager_match = re.search(r"<@([A-Z0-9]+)", form.get("text", ""))
        if not manager_match:
            return _ephemeral("Use `/leave-set-manager @manager`.")
        manager = _employee_by_slack(db, manager_match.group(1), form.get("team_id"))
        if manager is None:
            return _ephemeral("That manager is not registered in the leave system.")
        if manager.id == employee.id:
            return _ephemeral("You cannot assign yourself as your manager.")
        employee.manager_id = manager.id
        if manager.role == "employee":
            manager.role = "manager"
        db.commit()
        return _ephemeral(f"{manager.name} is now your manager.")

    return _ephemeral(
        "Available commands: `/leave-request`, `/leave-balance`, `/leave-history`, "
        "`/leave-admin`, and `/leave-set-manager @manager`."
    )


@router.post("/slack/interactions")
async def slack_interactions(
    request: Request,
    x_slack_signature: str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    raw_body = await request.body()
    _verify_slack_signature(raw_body, x_slack_signature, x_slack_request_timestamp)
    form = parse_qs(raw_body.decode("utf-8"))
    payload = json.loads(form.get("payload", ["{}"])[0])
    if payload.get("type") == "view_submission":
        callback_id = payload.get("view", {}).get("callback_id")
        if callback_id == "employee_balance_search_submission":
            return _submit_employee_balance_search(payload, db)
        if callback_id == "department_balance_submission":
            return _submit_department_balance(payload, db)
        if callback_id == "balance_adjustment_submission":
            return _submit_balance_adjustment(payload, db)
        if callback_id == "request_override_submission":
            return _submit_request_override(payload, db)
        return _submit_leave_modal(payload, db)
    if payload.get("type") == "block_suggestion":
        if payload.get("action_id") == "override_request_search":
            return _request_override_options(payload, db)
        return _employee_balance_options(payload, db)

    action = (payload.get("actions") or [{}])[0]
    action_id = action.get("action_id")
    if action_id in {
        "open_leave_request_modal",
        "open_balance_employee_search",
        "open_balance_department_filter",
        "open_balance_adjustment",
        "open_request_override",
    }:
        user_id = payload.get("user", {}).get("id", "")
        employee = _employee_by_slack(db, user_id, payload.get("team", {}).get("id"))
        if employee is None:
            return _ephemeral("Your Slack account is not registered in the leave system.")
        trigger_id = payload.get("trigger_id")
        if not trigger_id:
            return _ephemeral("Slack could not open the requested form. Please try again.")
        slack = RealSlackClient()
        if action_id == "open_leave_request_modal":
            _sync_policy_from_db(db)
            slack.open_leave_request_modal(trigger_id, leave_policy.all())
        elif employee.role not in {"manager", "hr", "admin"}:
            return _ephemeral("Only managers and HR can search employee balances.")
        elif action_id == "open_balance_employee_search":
            slack.open_employee_balance_search_modal(trigger_id)
        elif action_id == "open_balance_department_filter":
            departments = db.scalars(
                _visible_employee_query(employee)
                .with_only_columns(Employee.department)
                .where(Employee.department.is_not(None))
                .distinct()
                .order_by(Employee.department)
            ).all()
            slack.open_department_balance_modal(trigger_id, departments)
        elif employee.role not in {"hr", "admin"}:
            return _ephemeral("Only HR can manage balances or override requests.")
        elif action_id == "open_balance_adjustment":
            _sync_policy_from_db(db)
            slack.open_balance_adjustment_modal(trigger_id, leave_policy.all())
        else:
            slack.open_request_override_modal(trigger_id)
        return {"ok": True}

    interaction_id = action.get("action_ts") or payload.get("trigger_id") or hashlib.sha256(raw_body).hexdigest()
    enqueue_job(
        db,
        "process_slack_interaction",
        f"slack-interaction:{interaction_id}",
        {"interaction": payload},
    )
    db.commit()
    text = (
        "Preparing your leave report..."
        if action_id in {"balance_report_page", "download_balance_report"}
        else "Processing your decision..."
    )
    return {"response_type": "ephemeral", "text": text}


def _employee_balance_options(payload: dict, db: Session) -> dict:
    if payload.get("action_id") != "balance_employee_search":
        return {"options": []}
    requester = _employee_by_slack(
        db,
        payload.get("user", {}).get("id", ""),
        payload.get("team", {}).get("id"),
    )
    if requester is None or requester.role not in {"manager", "hr", "admin"}:
        return {"options": []}
    search = payload.get("value", "").strip()
    query = _visible_employee_query(requester).order_by(Employee.name).limit(100)
    if search:
        query = query.where(
            or_(
                Employee.name.ilike(f"%{search}%"),
                Employee.email.ilike(f"%{search}%"),
            )
        )
    employees = db.scalars(query).all()
    return {
        "options": [
            {
                "text": {"type": "plain_text", "text": employee.name[:75]},
                "value": str(employee.id),
            }
            for employee in employees
        ]
    }


def _submit_employee_balance_search(payload: dict, db: Session) -> dict:
    _sync_policy_from_db(db)
    requester = _employee_by_slack(
        db,
        payload.get("user", {}).get("id", ""),
        payload.get("team", {}).get("id"),
    )
    selected = _modal_value(
        payload.get("view", {}).get("state", {}).get("values", {}),
        "employee",
        "balance_employee_search",
        "selected_option",
        "value",
    )
    try:
        selected_id = int(selected)
    except (TypeError, ValueError):
        selected_id = None
    target = (
        db.scalar(_visible_employee_query(requester).where(Employee.id == selected_id))
        if requester and selected_id
        else None
    )
    if target is None:
        return _modal_errors({"employee": "Choose an employee you are allowed to view."})
    enqueue_job(
        db,
        "send_slack_message",
        f"balance-search:{payload.get('view', {}).get('id')}",
        {"channel": requester.slack_user_id, "text": _balance_result(db, target)["reply"]},
    )
    db.commit()
    return {"response_action": "clear"}


def _submit_department_balance(payload: dict, db: Session) -> dict:
    requester = _employee_by_slack(
        db,
        payload.get("user", {}).get("id", ""),
        payload.get("team", {}).get("id"),
    )
    if requester is None or requester.role not in {"manager", "hr", "admin"}:
        return _modal_errors({"department": "You are not allowed to view employee balances."})
    department = _modal_value(
        payload.get("view", {}).get("state", {}).get("values", {}),
        "department",
        "balance_department_select",
        "selected_option",
        "value",
    )
    enqueue_job(
        db,
        "send_balance_report_page",
        f"balance-department:{payload.get('view', {}).get('id')}",
        {
            "channel": requester.slack_user_id,
            "requester_id": requester.id,
            "department": None if department == "__all__" else department,
            "page": 0,
        },
    )
    db.commit()
    return {"response_action": "clear"}


def _submit_balance_adjustment(payload: dict, db: Session) -> dict:
    _sync_policy_from_db(db)
    requester = _employee_by_slack(
        db,
        payload.get("user", {}).get("id", ""),
        payload.get("team", {}).get("id"),
    )
    state = payload.get("view", {}).get("state", {}).get("values", {})
    selected = _modal_value(
        state,
        "employee",
        "balance_employee_search",
        "selected_option",
        "value",
    )
    leave_type = _modal_value(
        state,
        "leave_type",
        "adjustment_leave_type",
        "selected_option",
        "value",
    )
    days = _modal_value(state, "days", "adjustment_days", "value")
    reason = _modal_value(state, "reason", "adjustment_reason", "value")
    errors = {}
    try:
        employee_id = int(selected)
    except (TypeError, ValueError):
        employee_id = None
        errors["employee"] = "Choose an employee."
    try:
        days_delta = float(days)
        if days_delta == 0:
            errors["days"] = "The adjustment cannot be zero."
    except (TypeError, ValueError):
        days_delta = None
        errors["days"] = "Enter a valid number of days."
    if requester is None or requester.role not in {"hr", "admin"}:
        errors["employee"] = "Only HR can adjust balances."
    target = (
        db.scalar(_visible_employee_query(requester).where(Employee.id == employee_id))
        if requester and employee_id
        else None
    )
    if target is None:
        errors["employee"] = "Choose an employee you are allowed to manage."
    if leave_type not in leave_policy.all():
        errors["leave_type"] = "Choose a valid leave type."
    if not (reason or "").strip():
        errors["reason"] = "Enter a reason for this adjustment."
    if errors:
        return _modal_errors(errors)
    external_target_balance = None
    if settings.performance_api_mode.lower() == "live":
        rule = leave_policy.get(leave_type)
        try:
            balance = PerformanceAPIClient().find_balance(
                target.email,
                leave_type,
                rule.display_name,
            )
        except PerformanceAPIError as exc:
            return _modal_errors({"employee": str(exc)})
        if balance is None:
            return _modal_errors(
                {
                    "employee": (
                        f"{target.name} is not eligible for {rule.display_name}."
                    )
                }
            )
        external_target_balance = float(balance.get("balance") or 0) + days_delta
        if external_target_balance < 0 and not rule.allow_negative_balance:
            return _modal_errors(
                {"days": "This adjustment would make the remaining balance negative."}
            )
    enqueue_job(
        db,
        "adjust_leave_balance",
        f"balance-adjustment:{payload.get('view', {}).get('id')}",
        {
            "adjuster_id": requester.id,
            "employee_id": target.id,
            "leave_type": leave_type,
            "year": date.today().year,
            "days_delta": days_delta,
            "reason": reason.strip(),
            "reply_channel": requester.slack_user_id,
            "external_target_balance": external_target_balance,
        },
    )
    db.commit()
    return {"response_action": "clear"}


def _request_override_options(payload: dict, db: Session) -> dict:
    requester = _employee_by_slack(
        db,
        payload.get("user", {}).get("id", ""),
        payload.get("team", {}).get("id"),
    )
    if requester is None or requester.role not in {"hr", "admin"}:
        return {"options": []}
    search = payload.get("value", "").strip()
    query = (
        select(LeaveRequest)
        .join(Employee, LeaveRequest.employee_id == Employee.id)
        .where(Employee.workspace_id == requester.workspace_id)
        .order_by(LeaveRequest.id.desc())
        .limit(100)
    )
    if search:
        query = query.where(Employee.name.ilike(f"%{search}%"))
    requests = db.scalars(query).all()
    return {
        "options": [
            {
                "text": {
                    "type": "plain_text",
                    "text": (
                        f"{request.employee.name} | {leave_name(request.leave_type)} | "
                        f"{request.start_date} | {readable_status(request.status)}"
                    )[:75],
                },
                "value": str(request.id),
            }
            for request in requests
        ]
    }


def _submit_request_override(payload: dict, db: Session) -> dict:
    requester = _employee_by_slack(
        db,
        payload.get("user", {}).get("id", ""),
        payload.get("team", {}).get("id"),
    )
    state = payload.get("view", {}).get("state", {}).get("values", {})
    selected = _modal_value(
        state,
        "request",
        "override_request_search",
        "selected_option",
        "value",
    )
    status = _modal_value(
        state,
        "status",
        "override_status",
        "selected_option",
        "value",
    )
    reason = _modal_value(state, "reason", "override_reason", "value")
    try:
        request_id = int(selected)
    except (TypeError, ValueError):
        request_id = None
    request = (
        db.scalar(
            select(LeaveRequest)
            .join(Employee, LeaveRequest.employee_id == Employee.id)
            .where(
                LeaveRequest.id == request_id,
                Employee.workspace_id == requester.workspace_id,
            )
        )
        if requester and request_id
        else None
    )
    errors = {}
    if requester is None or requester.role not in {"hr", "admin"}:
        errors["request"] = "Only HR can override requests."
    elif request is None:
        errors["request"] = "Choose a request from your workspace."
    if status not in {"approved", "rejected", "cancelled"}:
        errors["status"] = "Choose a valid status."
    if not (reason or "").strip():
        errors["reason"] = "Enter a reason for the override."
    if errors:
        return _modal_errors(errors)
    enqueue_job(
        db,
        "override_leave_request",
        f"request-override:{payload.get('view', {}).get('id')}",
        {
            "approver_id": requester.id,
            "leave_request_id": request.id,
            "status": status,
            "reason": reason.strip(),
            "reply_channel": requester.slack_user_id,
        },
    )
    db.commit()
    return {"response_action": "clear"}


def _submit_leave_modal(payload: dict, db: Session) -> dict:
    view = payload.get("view", {})
    if view.get("callback_id") != "leave_request_submission":
        return {"response_action": "clear"}

    _sync_policy_from_db(db)
    user_id = payload.get("user", {}).get("id", "")
    employee = _employee_by_slack(db, user_id, payload.get("team", {}).get("id"))
    if employee is None:
        return _modal_errors({"leave_type": "Your Slack account is not registered."})
    if employee.manager is None:
        return _modal_errors({"leave_type": "Your manager has not been assigned yet."})

    state = view.get("state", {}).get("values", {})
    leave_type = _modal_value(state, "leave_type", "leave_type_select", "selected_option", "value")
    start_text = _modal_value(state, "start_date", "start_date_select", "selected_date")
    end_text = _modal_value(state, "end_date", "end_date_select", "selected_date")
    reason = _modal_value(state, "reason", "reason_input", "value")
    document_id = _modal_value(state, "document", "document_input", "files", 0, "id")
    errors = {}
    try:
        start_date = date.fromisoformat(start_text or "")
    except ValueError:
        start_date = None
        errors["start_date"] = "Choose a valid start date."
    try:
        end_date = date.fromisoformat(end_text or "")
    except ValueError:
        end_date = None
        errors["end_date"] = "Choose a valid end date."
    if start_date and start_date < date.today():
        errors["start_date"] = "The start date cannot be in the past."
    if start_date and end_date and end_date < start_date:
        errors["end_date"] = "The end date cannot be before the start date."
    if leave_type not in leave_policy.all():
        errors["leave_type"] = "Choose a valid leave type."
    elif leave_policy.get(leave_type).requires_document and not document_id:
        errors["document"] = "This leave policy requires a supporting document."
    if errors:
        return _modal_errors(errors)

    submission_id = view.get("id") or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    job_key = f"slack-modal:{submission_id}"
    if db.scalar(select(DurableJob).where(DurableJob.idempotency_key == job_key)):
        return {"response_action": "clear"}

    try:
        leave_request = LeaveRequestService(db).create_request(
            LeaveRequestCreate(
                employee_id=employee.id,
                leave_type=leave_type,
                start_date=start_date,
                end_date=end_date,
                reason=reason or None,
                document_key=f"slack:{document_id}" if document_id else None,
            )
        )
    except (ValueError, PerformanceAPIError) as exc:
        message = str(exc)
        field = "start_date" if "overlap" in message.lower() or "working days" in message.lower() else "leave_type"
        return _modal_errors({field: message})

    db.flush()
    if document_id:
        leave_request.status = "draft"
        enqueue_job(
            db,
            "upload_leave_document",
            job_key,
            {"leave_request_id": leave_request.id},
        )
    else:
        enqueue_job(
            db,
            (
                "create_external_leave_request"
                if settings.performance_api_mode.lower() == "live"
                else "start_agentspan"
            ),
            job_key,
            {"leave_request_id": leave_request.id},
        )
    enqueue_job(
        db,
        "send_slack_message",
        f"leave-confirmation:{submission_id}",
        {
            "channel": employee.slack_user_id,
            "text": (
                f"*Your {leave_policy.get(leave_type).display_name} request was received.*\n"
                + (
                    "I am processing the supporting document before notifying your manager."
                    if document_id
                    else (
                        "I am registering it in the leave system before notifying your manager."
                        if settings.performance_api_mode.lower() == "live"
                        else f"It was sent to {employee.manager.name} for approval."
                    )
                )
            ),
        },
    )
    db.commit()
    return {"response_action": "clear"}


def _modal_value(state: dict, block_id: str, action_id: str, *path: str):
    value = state.get(block_id, {}).get(action_id)
    for key in path:
        if isinstance(key, int) and isinstance(value, list) and len(value) > key:
            value = value[key]
        elif isinstance(key, str) and isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value


def _modal_errors(errors: dict[str, str]) -> dict:
    return {"response_action": "errors", "errors": errors}


def _ephemeral(text: str) -> dict[str, str]:
    return {"response_type": "ephemeral", "text": text}


def _process_chat(payload: ChatIn, db: Session) -> dict:
    _sync_policy_from_db(db)
    employee = _employee_by_slack(db, payload.slack_user_id, payload.workspace_id)
    if employee is None:
        return {
            "type": "unknown_user",
            "reply": "I do not know your employee record yet. Ask an admin to add your Slack user ID to the employee database.",
        }

    normalized = payload.text.lower()
    approval_result = _handle_chat_approval(normalized, employee, db)
    if approval_result is not None:
        return approval_result
    cancellation_match = re.search(
        r"\bcancel(?:lation)?\b(?:\s+(?:request|leave))?\s*#?\s*(\d+)",
        normalized,
    )
    if cancellation_match:
        return _handle_cancellation(employee, int(cancellation_match.group(1)), db)
    if "cancel" in normalized and "leave" in normalized:
        history = _history_result(db, employee)
        history["reply"] = (
            "*Choose a request to cancel below.*\n\n" + history["reply"]
        )
        return history
    if employee.role in {"hr", "admin"} and (
        ("adjust" in normalized and "balance" in normalized)
        or ("override" in normalized and ("request" in normalized or "leave" in normalized))
    ):
        count = (
            db.scalar(
                select(func.count()).select_from(
                    _visible_employee_query(employee).subquery()
                )
            )
            or 0
        )
        result = _balance_report_menu_result(count, can_manage=True)
        result["reply"] = (
            "*HR leave controls*\n"
            "*Adjust balance* adds or removes allocated days with a recorded reason. "
            "*Override request* changes a request's final status and records who made the change."
        )
        return result

    try:
        match = get_intent_router().classify(payload.text)
        logger.info(
            "Employee message classified",
            extra={"slack_user_id": employee.slack_user_id, "intent": match.intent},
        )
    except Exception:
        logger.warning("Intent router unavailable; showing employee menu", exc_info=True)
        match = None

    if match and match.intent == "request_leave":
        return {
            "type": "request_leave_prompt",
            "reply": (
                "I can help you submit a leave request. Click *Request leave* below "
                "to open a form where you can choose the leave type, start and end dates, "
                "and an optional reason."
            ),
        }
    if match and match.intent == "check_balance":
        return _balance_result_for_query(db, employee, payload.text)
    if match and match.intent == "leave_history":
        return _history_result(db, employee)
    if match and match.intent == "check_status":
        return _status_result(db, employee)
    if match and match.intent == "pending_requests":
        return _pending_requests_result(db, employee)
    return {
        "type": "employee_menu",
        "reply": (
            "Choose an action below. *Request leave* opens the leave form. "
            "*Check balance* shows allocated, used, and remaining days. "
            "*View history* shows your recent leave requests."
        ),
    }


def _verify_slack_signature(raw_body: bytes, signature: str | None, timestamp: str | None) -> None:
    if not settings.slack_signing_secret:
        raise HTTPException(status_code=500, detail="SLACK_SIGNING_SECRET is not configured")
    if not signature or not timestamp:
        raise HTTPException(status_code=401, detail="Missing Slack signature headers")
    try:
        request_time = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid Slack timestamp") from exc
    if abs(time.time() - request_time) > 60 * 5:
        raise HTTPException(status_code=401, detail="Stale Slack request")

    base = b"v0:" + timestamp.encode("utf-8") + b":" + raw_body
    expected = "v0=" + hmac.new(settings.slack_signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")


def _strip_bot_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


def _taken_balances(db: Session, employee: Employee) -> dict[str, float]:
    grouped = BalanceService(db).get_taken_days_for_employees([employee.id], date.today().year)
    return {leave_type: grouped.get(employee.id, {}).get(leave_type, 0.0) for leave_type in leave_policy.all()}


def _format_balance_reply(
    employee: Employee,
    balances: dict[str, float],
    allocations: dict[str, float] | None = None,
) -> str:
    rows = []
    for leave_type, used in balances.items():
        allocated = (allocations or {}).get(
            leave_type,
            leave_policy.get(leave_type).annual_days,
        )
        remaining = allocated - used
        filled = 10 if allocated == 0 and used else round(min(used / allocated, 1) * 10) if allocated else 0
        rows.append(
            f"*{leave_name(leave_type)}:* {used:g} used / {allocated:g} allocated / {remaining:g} remaining\n"
            f"`[{'#' * filled}{'-' * (10 - filled)}]`"
        )
    return f"*Leave balance for {employee.name} in {date.today().year}*\n" + "\n".join(rows)


def _format_compact_balance(
    employee: Employee,
    balances: dict[str, float],
    allocations: dict[str, float],
) -> str:
    values = []
    for leave_type, used in balances.items():
        allocated = allocations[leave_type]
        values.append(
            f"{leave_name(leave_type)}: {used:g}/{allocated:g} used, {allocated - used:g} remaining"
        )
    return f"*{employee.name}*\n" + " | ".join(values)


def _balance_result(db: Session, employee: Employee) -> dict:
    service = BalanceService(db)
    eligible = service.get_eligible_leave_types_for_employees([employee.id])[
        employee.id
    ]
    grouped = service.get_taken_days_for_employees(
        [employee.id],
        date.today().year,
    )
    balances = {
        leave_type: grouped.get(employee.id, {}).get(leave_type, 0.0)
        for leave_type in sorted(eligible)
    }
    allocations = service.get_allocated_days_for_employees(
        [employee.id],
        date.today().year,
    )[employee.id]
    allocations = {
        leave_type: allocations[leave_type]
        for leave_type in sorted(eligible)
    }
    if not eligible:
        return {
            "type": "balance",
            "reply": f"No leave balances were found for {employee.name}.",
            "balances": {},
            "allocations": {},
        }
    return {
        "type": "balance",
        "reply": _format_balance_reply(employee, balances, allocations),
        "balances": balances,
        "allocations": allocations,
    }


def _balance_result_for_query(db: Session, requester: Employee, text: str) -> dict:
    normalized = text.lower()
    if requester.role not in {"manager", "hr", "admin"}:
        return _balance_result(db, requester)

    team_words = ("team", "direct report", "employees", "everyone", "all balance", "all employee")
    if any(word in normalized for word in team_words):
        count = db.scalar(select(func.count()).select_from(_visible_employee_query(requester).subquery())) or 0
        if not count:
            return {"type": "balance", "reply": "You do not have any employees whose balance you can view."}
        return _balance_report_menu_result(count, requester.role in {"hr", "admin"})

    visible = db.scalars(_visible_employee_query(requester).order_by(Employee.name)).all()
    matches = [
        person
        for person in visible
        if person.name.lower() in normalized
        or any(len(part) >= 3 and part in normalized for part in person.name.lower().split())
    ]
    if len(matches) == 1:
        return _balance_result(db, matches[0])
    if len(matches) > 1:
        return {
            "type": "balance",
            "reply": "More than one employee matched that name. Please use the employee's full name.",
        }
    target_words = ("employee", "report", "staff member", "his", "her", "their", "someone")
    asks_for_self = bool(re.search(r"\b(my|mine)\b", normalized))
    if visible and (any(word in normalized for word in target_words) or not asks_for_self):
        return {
            **_balance_report_menu_result(
                len(visible),
                requester.role in {"hr", "admin"},
            ),
            "reply": (
                "*Which employee do you mean?*\n"
                "Please use the employee's name or click *Search employee* below."
            ),
        }
    return _balance_result(db, requester)


def _visible_employee_query(requester: Employee):
    query = select(Employee).where(
        Employee.is_active.is_(True),
        Employee.id != requester.id,
        Employee.workspace_id == requester.workspace_id,
    )
    if requester.role == "manager":
        query = query.where(Employee.manager_id == requester.id)
    return query


def _balance_report_menu_result(employee_count: int, can_manage: bool = False) -> dict:
    return {
        "type": "balance_report_menu",
        "can_manage": can_manage,
        "reply": (
            f"*Employee leave report*\n"
            f"*Employees:* {employee_count}\n"
            f"*Year:* {date.today().year}\n\n"
            "*Search employee* opens a name search. "
            "*View by department* shows 10 employees at a time. "
            "*Download CSV* creates the complete report."
        ),
    }


def _balance_report_page_result(
    db: Session,
    requester: Employee,
    department: str | None,
    page: int,
    page_size: int = 10,
) -> dict:
    query = _visible_employee_query(requester)
    if department:
        query = query.where(Employee.department == department)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 0), total_pages - 1)
    employees = db.scalars(
        query.order_by(Employee.name).offset(page * page_size).limit(page_size)
    ).all()
    grouped = BalanceService(db).get_taken_days_for_employees(
        [employee.id for employee in employees],
        date.today().year,
    )
    allocations = BalanceService(db).get_allocated_days_for_employees(
        [employee.id for employee in employees],
        date.today().year,
    )
    eligible = BalanceService(db).get_eligible_leave_types_for_employees(
        [employee.id for employee in employees],
    )
    rows = [
        _format_compact_balance(
            employee,
            {
                leave_type: grouped.get(employee.id, {}).get(leave_type, 0.0)
                for leave_type in sorted(eligible[employee.id])
            },
            {
                leave_type: allocations[employee.id][leave_type]
                for leave_type in sorted(eligible[employee.id])
            },
        )
        for employee in employees
    ]
    heading = department or "All employees"
    text = (
        f"*{heading} leave report*\n"
        f"*Page:* {page + 1} of {total_pages} | *Employees:* {total}\n\n"
        + ("\n\n".join(rows) if rows else "No employees found.")
    )
    return {
        "type": "balance_report_page",
        "reply": text,
        "department": department,
        "page": page,
        "total_pages": total_pages,
    }


def _pending_requests_result(db: Session, requester: Employee) -> dict:
    if requester.role not in {"manager", "hr", "admin"}:
        return {"type": "permission_denied", "reply": "Only managers and HR can view pending team requests."}

    query = (
        select(LeaveRequest)
        .join(Employee, LeaveRequest.employee_id == Employee.id)
        .where(
            LeaveRequest.status.in_(["pending_manager", "pending_hr"]),
            Employee.workspace_id == requester.workspace_id,
        )
    )
    if requester.role == "manager":
        query = query.where(Employee.manager_id == requester.id)
    requests = db.scalars(query.order_by(LeaveRequest.id.desc())).all()
    if not requests:
        return {"type": "pending_requests", "reply": "There are no pending leave requests for you."}
    rows = [
        f"*{item.employee.name}'s {leave_name(item.leave_type)} request*\n"
        f"*Dates:* {readable_date(item.start_date)} to {readable_date(item.end_date)}\n"
        f"*Working days:* {float(item.days_requested):g}\n"
        f"*Status:* {readable_status(item.status)}"
        for item in requests
    ]
    return {"type": "pending_requests", "reply": "*Pending leave requests*\n\n" + "\n\n".join(rows)}


def _history_result(db: Session, employee: Employee) -> dict:
    requests = db.scalars(
        select(LeaveRequest)
        .where(LeaveRequest.employee_id == employee.id)
        .order_by(LeaveRequest.id.desc())
        .limit(10)
    ).all()
    if not requests:
        return {"type": "history", "reply": "You do not have any leave requests yet."}
    rows = [
        f"*{leave_name(item.leave_type)}*\n"
        f"*Dates:* {readable_date(item.start_date)} to {readable_date(item.end_date)}\n"
        f"*Working days:* {float(item.days_requested):g}\n"
        f"*Status:* {readable_status(item.status)}"
        for item in requests
    ]
    cancellable_statuses = {
        LeaveRequestStatus.draft.value,
        LeaveRequestStatus.pending_manager.value,
        LeaveRequestStatus.pending_hr.value,
        LeaveRequestStatus.approved.value,
    }
    return {
        "type": "history",
        "reply": "*Your recent leave requests*\n\n" + "\n\n".join(rows),
        "cancellable_requests": [
            {
                "id": item.id,
                "status": item.status,
                "label": (
                    f"{leave_name(item.leave_type)}, "
                    f"{readable_date(item.start_date)} to {readable_date(item.end_date)}"
                ),
            }
            for item in requests
            if item.status in cancellable_statuses
        ],
    }


def _status_result(db: Session, employee: Employee) -> dict:
    request = db.scalar(
        select(LeaveRequest)
        .where(LeaveRequest.employee_id == employee.id)
        .order_by(LeaveRequest.id.desc())
        .limit(1)
    )
    if request is None:
        return {"type": "status", "reply": "You do not have a leave request to check yet."}
    return {
        "type": "status",
        "reply": (
            f"*Your latest leave request*\n"
            f"*Leave type:* {leave_name(request.leave_type)}\n"
            f"*Dates:* {readable_date(request.start_date)} to {readable_date(request.end_date)}\n"
            f"*Working days:* {float(request.days_requested):g}\n"
            f"*Status:* {readable_status(request.status)}"
        ),
    }


def _handle_chat_approval(text: str, approver: Employee, db: Session) -> dict | None:
    match = re.search(r"\b(approve|approved|reject|rejected|decline|declined)\b(?:\s+(?:request|leave))?\s*#?\s*(\d+)", text)
    if not match:
        return None

    approved = match.group(1).startswith("approv")
    request = db.get(LeaveRequest, int(match.group(2)))
    if request is None:
        return {"type": "not_found", "reply": "I could not find that leave request."}
    if not can_approve_request(approver, request):
        return {
            "type": "permission_denied",
            "reply": f"You are not allowed to decide {request.employee.name}'s leave request.",
        }

    return {
        "type": "approval_queued",
        "reply": (
            f"I am processing your decision for "
            f"*{request.employee.name}'s {leave_name(request.leave_type)} request*."
        ),
        "approved": approved,
        "approver_id": approver.id,
        "stage": "manager" if request.status == "pending_manager" else "hr",
        "request": LeaveRequestRead.model_validate(request).model_dump(mode="json"),
    }


def _handle_cancellation(employee: Employee, request_id: int, db: Session) -> dict:
    request = db.get(LeaveRequest, request_id)
    if request is None or request.employee_id != employee.id:
        return {
            "type": "not_found",
            "reply": "I could not find that leave request in your history.",
        }
    if request.status not in {
        LeaveRequestStatus.draft.value,
        LeaveRequestStatus.pending_manager.value,
        LeaveRequestStatus.pending_hr.value,
        LeaveRequestStatus.approved.value,
    }:
        return {
            "type": "invalid_cancellation",
            "reply": f"Your {leave_name(request.leave_type)} request is already {readable_status(request.status).lower()}.",
        }
    return {
        "type": "cancellation_queued",
        "reply": (
            f"I am processing cancellation of your "
            f"*{leave_name(request.leave_type)} request*."
        ),
        "request": LeaveRequestRead.model_validate(request).model_dump(mode="json"),
    }


def _employee_by_slack(db: Session, slack_user_id: str, workspace_id: str | None = None) -> Employee | None:
    query = select(Employee).where(Employee.slack_user_id == slack_user_id)
    if workspace_id:
        query = query.where(Employee.workspace_id == workspace_id)
    return db.scalar(query)


def _sync_policy_from_db(db: Session) -> LeavePolicyVersion:
    version = db.scalar(select(LeavePolicyVersion).order_by(LeavePolicyVersion.version.desc()))
    if version is None:
        version = LeavePolicyVersion(
            version=1,
            raw_text=leave_policy.to_raw_text(),
            rules_json=_policy_rules_json(),
            created_by="system_seed",
        )
        db.add(version)
        db.commit()
        db.refresh(version)
    else:
        leave_policy.load_raw_text(version.raw_text)
    return version


def _policy_rules_json() -> str:
    return json.dumps(
        {
            key: {
                "display_name": rule.display_name,
                "annual_days": rule.annual_days,
                "requires_document": rule.requires_document,
                "requires_hr": rule.requires_hr,
                "allow_negative_balance": rule.allow_negative_balance,
            }
            for key, rule in leave_policy.all().items()
        }
    )


@router.get("/employees/{employee_id}/balances/{leave_type}", response_model=BalanceRead)
def get_employee_balance(
    employee_id: int,
    leave_type: str,
    requester_slack_user_id: str,
    year: int | None = None,
    db: Session = Depends(get_db),
) -> BalanceRead:
    _sync_policy_from_db(db)
    requester = db.scalar(select(Employee).where(Employee.slack_user_id == requester_slack_user_id))
    target = db.get(Employee, employee_id)
    if requester is None or target is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not can_view_balance(requester, target):
        raise HTTPException(status_code=403, detail="Not allowed to view this balance")

    target_year = year or date.today().year
    balance_service = BalanceService(db)
    allocated = balance_service.get_allocated_days(employee_id, leave_type, target_year)
    taken = balance_service.get_taken_days(employee_id, leave_type, target_year)
    return BalanceRead(
        employee_id=employee_id,
        leave_type=leave_type,
        year=target_year,
        allocated_days=allocated,
        taken_days=taken,
        remaining_days=allocated - taken,
    )


@router.post("/requests/{request_id}/manager-decision", response_model=LeaveRequestRead)
def manager_decision(request_id: int, payload: DecisionIn, db: Session = Depends(get_db)) -> LeaveRequest:
    return _record_decision(request_id, payload, "manager", db)


@router.post("/requests/{request_id}/hr-decision", response_model=LeaveRequestRead)
def hr_decision(request_id: int, payload: DecisionIn, db: Session = Depends(get_db)) -> LeaveRequest:
    return _record_decision(request_id, payload, "hr", db)


def _record_decision(request_id: int, payload: DecisionIn, stage: str, db: Session) -> LeaveRequest:
    approver = db.scalar(select(Employee).where(Employee.slack_user_id == payload.approver_slack_user_id))
    request = db.get(LeaveRequest, request_id)
    if approver is None or request is None:
        raise HTTPException(status_code=404, detail="Approver or request not found")
    if not can_approve_request(approver, request):
        raise HTTPException(status_code=403, detail="Not allowed to approve this request")

    enqueue_job(
        db,
        "decide_agentspan",
        f"api-decision:{request.id}:{stage}:{approver.id}",
        {
            "leave_request_id": request.id,
            "approver_id": approver.id,
            "approved": payload.approved,
            "stage": stage,
            "reply_channel": approver.slack_user_id,
        },
    )
    db.commit()
    db.refresh(request)
    return request
