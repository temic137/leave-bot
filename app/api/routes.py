from datetime import date
import hashlib
import hmac
import json
import logging
import re
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Header, HTTPException, Request
import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.llm import GroqMessageParser
from app.adapters.slack import RealSlackClient
from app.core.config import settings
from app.db.models import ConversationSession, Employee, LeavePolicyVersion, LeaveRequest
from app.db.session import get_db
from app.schemas.leave import BalanceRead, LeaveRequestCreate, LeaveRequestRead, ParsedMessage
from app.services.balances import BalanceService
from app.services.employee_sync import EmployeeSyncService
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
    document_key: str | None = None


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
    action = (payload.get("actions") or [{}])[0]
    interaction_id = action.get("action_ts") or payload.get("trigger_id") or hashlib.sha256(raw_body).hexdigest()
    enqueue_job(
        db,
        "process_slack_interaction",
        f"slack-interaction:{interaction_id}",
        {"interaction": payload},
    )
    db.commit()
    return {"response_type": "ephemeral", "text": "Processing your decision..."}


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

    if _is_balance_query(normalized):
        target = _find_balance_target(db, employee, normalized)
        if not can_view_balance(employee, target):
            return {
                "type": "permission_denied",
                "reply": f"You are not allowed to view {target.name}'s leave balance.",
            }
        balances = _taken_balances(db, target)
        return {
            "type": "balance",
            "reply": _format_balance_reply(target, balances),
            "balances": balances,
        }

    if _is_leave_request(normalized) or _get_open_session(db, employee.slack_user_id) is not None:
        return _handle_leave_request_chat(payload, employee, db)

    return {
        "type": "help",
        "reply": "I can help you apply for leave or check leave taken. Try: I need annual leave from 2026-07-10 to 2026-07-12, or: show my leave balance.",
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


def _is_balance_query(text: str) -> bool:
    return any(phrase in text for phrase in ("balance", "leave taken", "taken leave", "how many leave", "how many days"))


def _is_leave_request(text: str) -> bool:
    return "leave" in text and any(word in text for word in ("apply", "request", "need", "want", "take", "off"))


def _find_balance_target(db: Session, requester: Employee, text: str) -> Employee:
    if any(phrase in text for phrase in ("my ", "me ", "i ", "mine")):
        return requester
    for employee in db.scalars(select(Employee)).all():
        if employee.name.lower() in text or employee.email.lower() in text:
            return employee
    return requester


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


def _handle_leave_request_chat(payload: ChatIn, employee: Employee, db: Session) -> dict:
    session = _get_open_session(db, employee.slack_user_id)
    existing_fields = _session_fields(session)
    parsed = _parse_leave_message_from_policy(payload.text, existing_fields)

    if parsed.missing_fields:
        _save_session(db, employee.slack_user_id, parsed)
        db.flush()
        missing = ", ".join(parsed.missing_fields)
        return {
            "type": "missing_fields",
            "reply": f"I still need: {missing}. You can say it naturally, for example: it starts on the 8th of July and ends on the 24th of July.",
            "parsed": parsed.model_dump(mode="json"),
        }

    rule = leave_policy.get(parsed.leave_type or "")
    if rule.requires_document and not payload.document_key:
        _save_session(db, employee.slack_user_id, parsed)
        db.flush()
        return {
            "type": "document_required",
            "reply": f"{rule.display_name} requires a document. Please attach a document before I send it for approval.",
            "parsed": parsed.model_dump(mode="json"),
        }

    if employee.manager is None:
        return {
            "type": "validation_error",
            "reply": "Your manager is not assigned yet. Ask an administrator to update your employee record before submitting leave.",
        }

    try:
        leave_request = LeaveRequestService(db).create_request(
            LeaveRequestCreate(
                employee_id=employee.id,
                leave_type=parsed.leave_type or "",
                start_date=parsed.start_date,
                end_date=parsed.end_date,
                reason=parsed.reason,
                document_key=payload.document_key,
            )
        )
    except ValueError as exc:
        return {"type": "validation_error", "reply": str(exc), "parsed": parsed.model_dump(mode="json")}

    _close_session(session)
    db.flush()

    manager = employee.manager.name if employee.manager else "your manager"
    route = f"sent to {manager}"
    if rule.requires_hr:
        route += ", and HR will review it after manager approval"

    return {
        "type": "leave_submitted",
        "reply": f"Your {rule.display_name} request for {float(leave_request.days_requested):g} day(s) has been recorded. I am sending it to {manager} now.",
        "request": LeaveRequestRead.model_validate(leave_request).model_dump(mode="json"),
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


def _get_open_session(db: Session, slack_user_id: str) -> ConversationSession | None:
    return db.scalar(
        select(ConversationSession).where(
            ConversationSession.slack_user_id == slack_user_id,
            ConversationSession.status == "open",
        )
    )


def _session_fields(session: ConversationSession | None) -> dict:
    if session is None or not session.collected_fields_json:
        return {}
    try:
        return json.loads(session.collected_fields_json)
    except json.JSONDecodeError:
        return {}


def _save_session(db: Session, slack_user_id: str, parsed: ParsedMessage) -> None:
    session = _get_open_session(db, slack_user_id)
    if session is None:
        session = ConversationSession(slack_user_id=slack_user_id, current_intent="create_leave_request")
        db.add(session)

    fields = _session_fields(session)
    for key in ("leave_type", "start_date", "end_date", "reason"):
        value = getattr(parsed, key)
        if value is not None:
            fields[key] = value.isoformat() if hasattr(value, "isoformat") else value
    session.collected_fields_json = json.dumps(fields)
    session.status = "open"


def _close_session(session: ConversationSession | None) -> None:
    if session is not None:
        session.status = "closed"


def _parse_leave_message_from_policy(text: str, existing_fields: dict | None = None) -> ParsedMessage:
    existing_fields = existing_fields or {}
    if settings.groq_api_key:
        try:
            parsed = GroqMessageParser().parse(
                text,
                leave_types=list(leave_policy.all()),
                existing_fields=existing_fields,
            )
            if parsed.leave_type in leave_policy.all() or parsed.leave_type is None:
                return parsed
        except (httpx.HTTPError, KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError):
            logger.warning("Groq parser unavailable; using deterministic fallback", exc_info=True)

    normalized = text.lower()
    leave_type = existing_fields.get("leave_type")
    for key, rule in leave_policy.all().items():
        candidates = {key.lower(), key.replace("_", " ").lower(), rule.display_name.lower()}
        tokens = {
            token
            for candidate in candidates
            for token in re.findall(r"[a-z]+", candidate)
            if token not in {"leave", "days", "day", "maximum"}
        }
        if any(candidate in normalized for candidate in candidates) or any(token in normalized for token in tokens):
            leave_type = key
            break

    parsed_dates = _parse_dates(text)
    start_date = parsed_dates[0] if len(parsed_dates) >= 1 else _date_from_existing(existing_fields.get("start_date"))
    end_date = parsed_dates[1] if len(parsed_dates) >= 2 else _date_from_existing(existing_fields.get("end_date"))
    if end_date is None and len(parsed_dates) == 1 and start_date is not None:
        end_date = start_date

    missing = []
    if leave_type is None:
        missing.append("leave_type")
    if start_date is None:
        missing.append("start_date")
    if end_date is None:
        missing.append("end_date")

    return ParsedMessage(
        intent="create_leave_request",
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        reason=existing_fields.get("reason") or text,
        confidence=0.7 if not missing else 0.35,
        missing_fields=missing,
    )


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


def _date_from_existing(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _parse_dates(text: str) -> list[date]:
    dates = [date.fromisoformat(value) for value in re.findall(r"\d{4}-\d{2}-\d{2}", text)]
    if dates:
        return dates

    months = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }
    found: list[date] = []
    pattern = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?\s+([a-zA-Z]+)(?:\s+(\d{4}))?\b")
    for day_text, month_text, year_text in pattern.findall(text):
        month = months.get(month_text.lower())
        if not month:
            continue
        found.append(date(int(year_text or date.today().year), month, int(day_text)))
    return found


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
