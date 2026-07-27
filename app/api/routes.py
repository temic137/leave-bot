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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.slack import RealSlackClient
from app.core.config import settings
from app.db.models import DurableJob, Employee, LeavePolicyVersion, LeaveRequest
from app.db.session import get_db
from app.schemas.leave import BalanceRead, LeaveRequestCreate, LeaveRequestRead
from app.services.balances import BalanceService
from app.services.employee_sync import EmployeeSyncService
from app.services.intents import get_intent_router
from app.services.leave_requests import LeaveRequestService
from app.services.jobs import enqueue_job
from app.services.permissions import can_approve_request, can_view_balance
from app.services.policy import leave_policy


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


class PolicyTextIn(BaseModel):
    text: str


class ChatIn(BaseModel):
    slack_user_id: str
    text: str


class EmployeeIn(BaseModel):
    name: str
    email: str
    slack_user_id: str
    role: str = "employee"
    department: str | None = None
    manager_id: int | None = None


@router.post("/admin/init-db")
def init_db() -> dict[str, str]:
    return {"status": "managed by Alembic migrations"}


@router.post("/admin/sync/slack")
def sync_real_slack(db: Session = Depends(get_db)) -> dict[str, int]:
    slack = RealSlackClient()
    service = EmployeeSyncService(db)
    count = 0
    for user in slack.list_users():
        service.upsert_slack_user(user.slack_user_id, user.email, user.name, user.is_active)
        count += 1
    db.commit()
    return {"users_upserted": count}


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
            slack_user_id=payload.slack_user_id,
            email=payload.email,
            name=payload.name,
            role=payload.role,
            department=payload.department,
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
    db.flush()
    db.commit()
    db.refresh(employee)
    return {
        "id": employee.id,
        "slack_user_id": employee.slack_user_id,
        "email": employee.email,
        "name": employee.name,
        "role": employee.role,
        "department": employee.department,
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

    return {
        "employees": [
            {
                "id": employee.id,
                "slack_user_id": employee.slack_user_id,
                "name": employee.name,
                "email": employee.email,
                "role": employee.role,
                "department": employee.department,
                "manager_id": employee.manager_id,
                "manager_name": employee.manager.name if employee.manager else None,
                "balances": {
                    leave_type: balance_service.get_taken_days(employee.id, leave_type, target_year)
                    for leave_type in leave_policy.all()
                },
            }
            for employee in employees
        ],
        "requests": [
            {
                "id": request.id,
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
    employee = db.scalar(select(Employee).where(Employee.slack_user_id == user_id))
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
        query = select(LeaveRequest).where(LeaveRequest.status.in_(["pending_manager", "pending_hr"]))
        if employee.role not in {"hr", "admin"}:
            query = query.join(Employee, LeaveRequest.employee_id == Employee.id).where(Employee.manager_id == employee.id)
        requests = db.scalars(query.order_by(LeaveRequest.id.desc())).all()
        if not requests:
            return _ephemeral("There are no pending leave requests for you.")
        rows = [
            f"#{item.id} | {item.employee.name} | {item.leave_type} | "
            f"{item.start_date} to {item.end_date} | {item.status.replace('_', ' ')}"
            for item in requests
        ]
        return _ephemeral("*Pending leave requests*\n" + "\n".join(rows))

    if command == "/leave-set-manager":
        manager_match = re.search(r"<@([A-Z0-9]+)", form.get("text", ""))
        if not manager_match:
            return _ephemeral("Use `/leave-set-manager @manager`.")
        manager = db.scalar(select(Employee).where(Employee.slack_user_id == manager_match.group(1)))
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
        return _submit_leave_modal(payload, db)

    action = (payload.get("actions") or [{}])[0]
    if action.get("action_id") == "open_leave_request_modal":
        user_id = payload.get("user", {}).get("id", "")
        employee = db.scalar(select(Employee).where(Employee.slack_user_id == user_id))
        if employee is None:
            return _ephemeral("Your Slack account is not registered in the leave system.")
        trigger_id = payload.get("trigger_id")
        if not trigger_id:
            return _ephemeral("Slack could not open the form. Please use `/leave-request`.")
        _sync_policy_from_db(db)
        RealSlackClient().open_leave_request_modal(trigger_id, leave_policy.all())
        return {"ok": True}

    interaction_id = action.get("action_ts") or payload.get("trigger_id") or hashlib.sha256(raw_body).hexdigest()
    enqueue_job(
        db,
        "process_slack_interaction",
        f"slack-interaction:{interaction_id}",
        {"interaction": payload},
    )
    db.commit()
    return {"response_type": "ephemeral", "text": "Processing your decision..."}


def _submit_leave_modal(payload: dict, db: Session) -> dict:
    view = payload.get("view", {})
    if view.get("callback_id") != "leave_request_submission":
        return {"response_action": "clear"}

    _sync_policy_from_db(db)
    user_id = payload.get("user", {}).get("id", "")
    employee = db.scalar(select(Employee).where(Employee.slack_user_id == user_id))
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
    except ValueError as exc:
        return _modal_errors({"leave_type": str(exc)})

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
            "start_agentspan",
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
                f"Your {leave_policy.get(leave_type).display_name} request #{leave_request.id} was received. "
                + (
                    "I am processing the supporting document before notifying your manager."
                    if document_id
                    else f"It was submitted to {employee.manager.name}."
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
    employee = db.scalar(select(Employee).where(Employee.slack_user_id == payload.slack_user_id))
    if employee is None:
        return {
            "type": "unknown_user",
            "reply": "I do not know your employee record yet. Ask an admin to add your Slack user ID to the employee database.",
        }

    normalized = payload.text.lower()
    approval_result = _handle_chat_approval(normalized, employee, db)
    if approval_result is not None:
        return approval_result

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
            "*Check balance* shows your approved days taken. "
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
    balance_service = BalanceService(db)
    target_year = date.today().year
    return {
        leave_type: balance_service.get_taken_days(employee.id, leave_type, target_year)
        for leave_type in leave_policy.all()
    }


def _format_balance_reply(employee: Employee, balances: dict[str, float]) -> str:
    rows = ", ".join(f"{leave_type}: {days:g} days taken" for leave_type, days in balances.items())
    return f"{employee.name}'s leave taken this year: {rows}."


def _balance_result(db: Session, employee: Employee) -> dict:
    balances = _taken_balances(db, employee)
    return {
        "type": "balance",
        "reply": _format_balance_reply(employee, balances),
        "balances": balances,
    }


def _balance_result_for_query(db: Session, requester: Employee, text: str) -> dict:
    normalized = text.lower()
    if requester.role not in {"manager", "hr", "admin"}:
        return _balance_result(db, requester)

    query = select(Employee).where(Employee.is_active.is_(True), Employee.id != requester.id)
    if requester.role == "manager":
        query = query.where(Employee.manager_id == requester.id)
    visible = db.scalars(query.order_by(Employee.name)).all()

    team_words = ("team", "direct report", "employees", "everyone", "all balance")
    if any(word in normalized for word in team_words):
        if not visible:
            return {"type": "balance", "reply": "You do not have any employees whose balance you can view."}
        rows = [_format_balance_reply(person, _taken_balances(db, person)) for person in visible]
        return {"type": "balance", "reply": "*Employee leave taken this year*\n" + "\n".join(rows)}

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
    if visible and any(word in normalized for word in ("employee", "report", "staff member")):
        names = ", ".join(person.name for person in visible)
        return {"type": "balance", "reply": f"Whose balance do you want to see? You can ask about: {names}."}
    return _balance_result(db, requester)


def _pending_requests_result(db: Session, requester: Employee) -> dict:
    if requester.role not in {"manager", "hr", "admin"}:
        return {"type": "permission_denied", "reply": "Only managers and HR can view pending team requests."}

    query = select(LeaveRequest).where(LeaveRequest.status.in_(["pending_manager", "pending_hr"]))
    if requester.role == "manager":
        query = query.join(Employee, LeaveRequest.employee_id == Employee.id).where(
            Employee.manager_id == requester.id
        )
    requests = db.scalars(query.order_by(LeaveRequest.id.desc())).all()
    if not requests:
        return {"type": "pending_requests", "reply": "There are no pending leave requests for you."}
    rows = [
        f"#{item.id} | {item.employee.name} | {item.leave_type} | "
        f"{item.start_date} to {item.end_date} | {item.status.replace('_', ' ')}"
        for item in requests
    ]
    return {"type": "pending_requests", "reply": "*Pending leave requests*\n" + "\n".join(rows)}


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
        f"#{item.id} | {item.leave_type} | {item.start_date} to {item.end_date} | "
        f"{float(item.days_requested):g} day(s) | {item.status.replace('_', ' ')}"
        for item in requests
    ]
    return {"type": "history", "reply": "*Your recent leave requests*\n" + "\n".join(rows)}


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
            f"Your latest request is #{request.id}: {request.leave_type}, "
            f"{request.start_date} to {request.end_date}, "
            f"status: *{request.status.replace('_', ' ')}*."
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
        return {"type": "permission_denied", "reply": f"{approver.name} is not allowed to approve or reject request #{request.id}."}

    return {
        "type": "approval_queued",
        "reply": f"I am processing your decision for request #{request.id}.",
        "approved": approved,
        "approver_id": approver.id,
        "stage": "manager" if request.status == "pending_manager" else "hr",
        "request": LeaveRequestRead.model_validate(request).model_dump(mode="json"),
    }


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
    requester = db.scalar(select(Employee).where(Employee.slack_user_id == requester_slack_user_id))
    target = db.get(Employee, employee_id)
    if requester is None or target is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not can_view_balance(requester, target):
        raise HTTPException(status_code=403, detail="Not allowed to view this balance")

    target_year = year or date.today().year
    balance_service = BalanceService(db)
    return BalanceRead(
        employee_id=employee_id,
        leave_type=leave_type,
        year=target_year,
        taken_days=balance_service.get_taken_days(employee_id, leave_type, target_year),
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
