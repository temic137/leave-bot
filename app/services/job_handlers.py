import csv
from datetime import date
import io
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.slack import RealSlackClient
from app.adapters.performance import PerformanceAPIClient
from app.adapters.storage import AutochekDocumentStorage
from app.adapters.workflow import AgentSpanApprovalWorkflow
from app.core.config import settings
from app.db.models import DurableJob, Employee, LeaveRequest, LeaveRequestStatus
from app.services.balances import BalanceService
from app.services.employee_sync import EmployeeSyncService
from app.services.jobs import PermanentJobError, enqueue_job
from app.services.leave_requests import LeaveRequestService
from app.services.permissions import can_approve_request
from app.services.presentation import leave_name, readable_date, readable_status


logger = logging.getLogger(__name__)


def handle_job(db: Session, job: DurableJob) -> None:
    handlers = {
        "process_slack_event": _process_slack_event,
        "process_slack_interaction": _process_slack_interaction,
        "start_agentspan": _start_agentspan,
        "create_external_leave_request": _create_external_leave_request,
        "update_external_leave_request": _update_external_leave_request,
        "sync_external_employees": _sync_external_employees,
        "decide_agentspan": _decide_agentspan,
        "send_slack_message": _send_slack_message,
        "send_leave_request_prompt": _send_leave_request_prompt,
        "send_employee_menu": _send_employee_menu,
        "send_leave_history": _send_leave_history,
        "send_balance_report_menu": _send_balance_report_menu,
        "send_balance_report_page": _send_balance_report_page,
        "send_balance_report_csv": _send_balance_report_csv,
        "publish_employee_home": _publish_employee_home,
        "send_approval_card": _send_approval_card,
        "send_cancellation_card": _send_cancellation_card,
        "update_leave_cards": _update_leave_cards,
        "cancel_leave_request": _cancel_leave_request,
        "decide_leave_cancellation": _decide_leave_cancellation,
        "adjust_leave_balance": _adjust_leave_balance,
        "override_leave_request": _override_leave_request,
        "upload_leave_document": _upload_leave_document,
    }
    handler = handlers.get(job.job_type)
    if handler is None:
        raise PermanentJobError(f"Unknown job type: {job.job_type}")
    handler(db, job, json.loads(job.payload_json))


def _process_slack_event(db: Session, job: DurableJob, payload: dict) -> None:
    from app.api.routes import ChatIn, _process_chat, _strip_bot_mention

    slack_payload = payload["slack_payload"]
    event = slack_payload.get("event", {})
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("type") == "app_home_opened" and event.get("user"):
        enqueue_job(
            db,
            "publish_employee_home",
            f"app-home:{event['user']}:{slack_payload.get('event_time', job.id)}",
            {"slack_user_id": event["user"]},
        )
        return
    if event.get("type") not in {"message", "app_mention"}:
        return
    user_id = event.get("user")
    channel_id = event.get("channel")
    text = _strip_bot_mention(event.get("text", ""))
    if not user_id or not channel_id or not text:
        return

    result = _process_chat(
        ChatIn(
            slack_user_id=user_id,
            text=text,
            workspace_id=slack_payload.get("team_id") or event.get("team"),
        ),
        db,
    )
    _queue_chat_result(db, job, channel_id, result)
    logger.info(
        "Slack event processed",
        extra={
            "job_id": job.id,
            "slack_event_id": slack_payload.get("event_id"),
            "slack_user_id": user_id,
        },
    )


def _process_slack_interaction(db: Session, job: DurableJob, payload: dict) -> None:
    from app.api.routes import (
        _balance_report_page_result,
        _balance_result,
        _handle_cancellation,
        _handle_chat_approval,
        _history_result,
    )

    interaction = payload["interaction"]
    user_id = interaction.get("user", {}).get("id")
    action = (interaction.get("actions") or [{}])[0]
    request_id = action.get("value")
    action_id = action.get("action_id")
    if not user_id:
        raise PermanentJobError("Slack interaction has no user")
    from app.api.routes import _employee_by_slack

    employee = _employee_by_slack(
        db,
        user_id,
        interaction.get("team", {}).get("id"),
    )
    if employee is None:
        _queue_message(db, f"interaction-reply:{job.id}", user_id, "Your Slack account is not registered.")
        return
    if action_id == "check_leave_balance":
        _queue_chat_result(db, job, user_id, _balance_result(db, employee))
        return
    if action_id == "view_leave_history":
        _queue_chat_result(db, job, user_id, _history_result(db, employee))
        return
    if action_id == "cancel_leave":
        try:
            leave_request_id = int(request_id)
        except (TypeError, ValueError) as exc:
            raise PermanentJobError("Invalid cancellation request") from exc
        _queue_chat_result(
            db,
            job,
            user_id,
            _handle_cancellation(employee, leave_request_id, db),
        )
        return
    if action_id in {
        "approve_leave_cancellation",
        "reject_leave_cancellation",
    }:
        try:
            leave_request_id = int(request_id)
        except (TypeError, ValueError) as exc:
            raise PermanentJobError("Invalid cancellation decision") from exc
        enqueue_job(
            db,
            "decide_leave_cancellation",
            f"cancellation-decision:{job.id}",
            {
                "leave_request_id": leave_request_id,
                "approver_id": employee.id,
                "approved": action_id == "approve_leave_cancellation",
                "reply_channel": user_id,
                "message_ref": _interaction_message_ref(interaction),
            },
        )
        _queue_message(
            db,
            f"cancellation-decision-started:{job.id}",
            user_id,
            "I am processing your cancellation decision.",
        )
        return
    if action_id in {"balance_report_page", "download_balance_report"} and employee.role not in {
        "manager",
        "hr",
        "admin",
    }:
        _queue_message(
            db,
            f"balance-report-denied:{job.id}",
            user_id,
            "Only managers and HR can view employee balance reports.",
        )
        return
    if action_id == "balance_report_page":
        try:
            selection = json.loads(request_id or "{}")
        except json.JSONDecodeError as exc:
            raise PermanentJobError("Invalid balance report page") from exc
        result = _balance_report_page_result(
            db,
            employee,
            selection.get("department"),
            int(selection.get("page", 0)),
        )
        _queue_chat_result(db, job, user_id, result)
        return
    if action_id == "download_balance_report":
        enqueue_job(
            db,
            "send_balance_report_csv",
            f"balance-csv:{job.id}",
            {"channel": user_id, "requester_id": employee.id},
        )
        _queue_message(
            db,
            f"balance-csv-started:{job.id}",
            user_id,
            "I am preparing your employee leave report as a CSV file.",
        )
        return
    if not request_id or action_id not in {"approve_leave", "reject_leave"}:
        raise PermanentJobError("Invalid Slack interaction")
    verb = "approve" if action_id == "approve_leave" else "reject"
    result = _handle_chat_approval(f"{verb} request {request_id}", employee, db)
    if result is None:
        result = {"type": "invalid_approval", "reply": "The approval could not be processed."}
    else:
        result["message_ref"] = _interaction_message_ref(interaction)
    _queue_chat_result(db, job, user_id, result)


def _queue_chat_result(db: Session, source_job: DurableJob, reply_channel: str, result: dict) -> None:
    if result.get("type") == "request_leave_prompt":
        enqueue_job(
            db,
            "send_leave_request_prompt",
            f"chat-reply:{source_job.id}",
            {"channel": reply_channel, "text": result["reply"]},
        )
        return
    if result.get("type") == "employee_menu":
        enqueue_job(
            db,
            "send_employee_menu",
            f"chat-reply:{source_job.id}",
            {"channel": reply_channel, "text": result["reply"]},
        )
        return
    if result.get("type") == "history":
        enqueue_job(
            db,
            "send_leave_history",
            f"chat-reply:{source_job.id}",
            {
                "channel": reply_channel,
                "text": result["reply"],
                "cancellable_requests": result.get("cancellable_requests", []),
            },
        )
        return
    if result.get("type") == "balance_report_menu":
        enqueue_job(
            db,
            "send_balance_report_menu",
            f"chat-reply:{source_job.id}",
            {
                "channel": reply_channel,
                "text": result["reply"],
                "can_manage": result.get("can_manage", False),
            },
        )
        return
    if result.get("type") == "balance_report_page":
        enqueue_job(
            db,
            "send_balance_report_page",
            f"chat-reply:{source_job.id}",
            {
                "channel": reply_channel,
                "requester_id": result.get("requester_id"),
                "department": result["department"],
                "page": result["page"],
                "text": result["reply"],
                "total_pages": result["total_pages"],
            },
        )
        return

    _queue_message(db, f"chat-reply:{source_job.id}", reply_channel, result["reply"])
    if result.get("type") == "leave_submitted":
        request_id = result["request"]["id"]
        enqueue_job(
            db,
            "start_agentspan",
            f"agentspan-start:leave-request:{request_id}",
            {"leave_request_id": request_id},
        )
    elif result.get("type") == "approval_queued":
        request_id = result["request"]["id"]
        enqueue_job(
            db,
            "decide_agentspan",
            f"agentspan-decision:{source_job.id}",
            {
                "leave_request_id": request_id,
                "approver_id": result["approver_id"],
                "approved": result["approved"],
                "stage": result["stage"],
                "reply_channel": reply_channel,
                "message_ref": result.get("message_ref"),
            },
        )
    elif result.get("type") == "cancellation_queued":
        enqueue_job(
            db,
            "cancel_leave_request",
            f"leave-cancellation:{source_job.id}",
            {
                "leave_request_id": result["request"]["id"],
                "employee_id": result["request"]["employee_id"],
                "reply_channel": reply_channel,
            },
        )


def _interaction_message_ref(interaction: dict) -> dict | None:
    channel = interaction.get("channel", {}).get("id")
    timestamp = interaction.get("message", {}).get("ts")
    return {"channel": channel, "ts": timestamp} if channel and timestamp else None


def _start_agentspan(db: Session, job: DurableJob, payload: dict) -> None:
    request = db.scalar(
        select(LeaveRequest).where(LeaveRequest.id == payload["leave_request_id"]).with_for_update()
    )
    if request is None:
        raise PermanentJobError("Leave request no longer exists")
    if request.employee.manager is None:
        raise PermanentJobError("Employee has no manager")
    if not request.agentspan_execution_id:
        from app.services.policy import leave_policy

        requires_hr = leave_policy.get(request.leave_type).requires_hr
        workflow = AgentSpanApprovalWorkflow()
        workflow.ensure_registered(requires_hr)
        handle = workflow.start(request.id, requires_hr)
        request.agentspan_execution_id = handle.execution_id
        db.flush()
    enqueue_job(
        db,
        "send_approval_card",
        f"manager-approval-card:{request.id}",
        {"leave_request_id": request.id, "recipient_slack_user_id": request.employee.manager.slack_user_id},
    )
    logger.info(
        "AgentSpan workflow started",
        extra={
            "job_id": job.id,
            "leave_request_id": request.id,
            "agentspan_execution_id": request.agentspan_execution_id,
        },
    )


def _create_external_leave_request(
    db: Session,
    job: DurableJob,
    payload: dict,
) -> None:
    request = db.scalar(
        select(LeaveRequest)
        .where(LeaveRequest.id == payload["leave_request_id"])
        .with_for_update()
    )
    if request is None:
        raise PermanentJobError("Leave request no longer exists")
    _ensure_external_request(request)
    db.flush()
    enqueue_job(
        db,
        "start_agentspan",
        f"agentspan-start:leave-request:{request.id}",
        {"leave_request_id": request.id},
    )


def _update_external_leave_request(
    db: Session,
    job: DurableJob,
    payload: dict,
) -> None:
    request = db.scalar(
        select(LeaveRequest)
        .where(LeaveRequest.id == payload["leave_request_id"])
        .with_for_update()
    )
    approver = db.get(Employee, payload["approver_id"])
    if request is None or approver is None:
        raise PermanentJobError("Approver or leave request no longer exists")
    _ensure_external_request(request)
    PerformanceAPIClient().update_leave_request(
        request.external_request_id,
        status=_external_status(request.status),
        approver_name=approver.name,
        approver_email=approver.email,
    )


def _sync_external_employees(db: Session, job: DurableJob, payload: dict) -> None:
    service = EmployeeSyncService(db)
    for record in PerformanceAPIClient().list_employees():
        service.upsert_external_employee(record)


def _ensure_external_request(request: LeaveRequest) -> None:
    if request.external_request_id:
        return
    if request.employee.manager is None:
        raise PermanentJobError("Employee has no manager")
    from app.services.policy import leave_policy

    client = PerformanceAPIClient()
    rule = leave_policy.get(request.leave_type)
    balance = client.find_balance(
        request.employee.email,
        request.leave_type,
        rule.display_name,
    )
    if balance is None:
        raise PermanentJobError(
            f"{request.employee.name} is not eligible for {rule.display_name}"
        )
    external_leave_type = str(balance["leavetype"])
    existing = client.find_request(
        email=request.employee.email,
        leave_type=external_leave_type,
        start_date=request.start_date,
        end_date=request.end_date,
    )
    record = existing or client.create_leave_request(
        email=request.employee.email,
        employee_name=request.employee.name,
        leave_type=external_leave_type,
        start_date=request.start_date,
        end_date=request.end_date,
        status=_external_status(request.status),
        country=str(balance.get("country") or request.employee.country or ""),
        approver_name=request.employee.manager.name,
        approver_email=request.employee.manager.email,
    )
    if not record.get("id"):
        raise RuntimeError("Performance API did not return a leave request ID")
    request.external_request_id = str(record["id"])
    request.external_leave_type = external_leave_type


def _external_status(local_status: str) -> str:
    if local_status == LeaveRequestStatus.approved.value:
        return "Approved"
    if local_status in {
        LeaveRequestStatus.rejected.value,
        LeaveRequestStatus.cancelled.value,
    }:
        return "Declined"
    return "Pending"


def _queue_external_update(
    db: Session,
    request: LeaveRequest,
    approver: Employee,
) -> None:
    if settings.performance_api_mode.lower() != "live":
        return
    version = (
        request.decided_at.isoformat()
        if request.decided_at is not None
        else request.status
    )
    enqueue_job(
        db,
        "update_external_leave_request",
        f"external-request-status:{request.id}:{request.status}:{version}",
        {
            "leave_request_id": request.id,
            "approver_id": approver.id,
        },
    )


def _decide_agentspan(db: Session, job: DurableJob, payload: dict) -> None:
    request = db.scalar(
        select(LeaveRequest).where(LeaveRequest.id == payload["leave_request_id"]).with_for_update()
    )
    approver = db.get(Employee, payload["approver_id"])
    if request is None or approver is None:
        raise PermanentJobError("Approver or leave request no longer exists")

    expected_status = "pending_manager" if payload["stage"] == "manager" else "pending_hr"
    if request.status != expected_status:
        _queue_leave_card_update(
            db,
            f"approval-card-stale:{job.id}",
            request,
            payload.get("message_ref"),
            ["approval:", "clicked"],
        )
        _queue_message(
            db,
            f"decision-result:{job.id}",
            payload["reply_channel"],
            (
                f"*{request.employee.name}'s {leave_name(request.leave_type)} request* "
                f"is already {readable_status(request.status).lower()}."
            ),
        )
        return
    if not can_approve_request(approver, request):
        _queue_message(
            db,
            f"decision-result:{job.id}",
            payload["reply_channel"],
            f"You are not allowed to decide {request.employee.name}'s leave request.",
        )
        return
    if not request.agentspan_execution_id:
        raise RuntimeError("AgentSpan workflow has not started yet")

    AgentSpanApprovalWorkflow().decide(
        request.agentspan_execution_id,
        payload["approved"],
        "Rejected from Slack" if not payload["approved"] else "",
        stage=payload["stage"],
    )
    service = LeaveRequestService(db)
    if payload["stage"] == "manager":
        service.record_manager_decision(approver, request, payload["approved"], "Slack decision")
    else:
        service.record_hr_decision(approver, request, payload["approved"], "Slack decision")
    db.flush()
    if request.status in {
        LeaveRequestStatus.approved.value,
        LeaveRequestStatus.rejected.value,
    }:
        _queue_external_update(db, request, approver)
    _queue_leave_card_update(
        db,
        f"approval-card-update:{job.id}",
        request,
        payload.get("message_ref"),
        ["approval:", "clicked"],
    )

    decision_text = "approved" if payload["approved"] else "rejected"
    _queue_message(
        db,
        f"decision-result:{job.id}",
        payload["reply_channel"],
        f"*{request.employee.name}'s {leave_name(request.leave_type)} request* has been {decision_text}.",
    )
    if request.status in {"approved", "rejected"}:
        _queue_message(
            db,
            f"employee-final-decision:{request.id}:{request.status}",
            request.employee.slack_user_id,
            (
                f"*Your {leave_name(request.leave_type)} request has been {request.status}.*\n"
                f"*Dates:* {readable_date(request.start_date)} to {readable_date(request.end_date)}"
            ),
        )
    elif request.status == "pending_hr":
        hr_people = db.scalars(
            select(Employee).where(
                Employee.role.in_(["hr", "admin"]),
                Employee.is_active.is_(True),
                Employee.workspace_id == request.employee.workspace_id,
            )
        ).all()
        for hr in hr_people:
            enqueue_job(
                db,
                "send_approval_card",
                f"hr-approval-card:{request.id}:{hr.id}",
                {"leave_request_id": request.id, "recipient_slack_user_id": hr.slack_user_id},
            )


def _send_slack_message(db: Session, job: DurableJob, payload: dict) -> None:
    RealSlackClient().send_channel_message(payload["channel"], payload["text"])


def _send_leave_request_prompt(db: Session, job: DurableJob, payload: dict) -> None:
    RealSlackClient().send_leave_request_prompt(payload["channel"], payload["text"])


def _send_employee_menu(db: Session, job: DurableJob, payload: dict) -> None:
    RealSlackClient().send_employee_menu(payload["channel"], payload["text"])


def _send_leave_history(db: Session, job: DurableJob, payload: dict) -> None:
    RealSlackClient().send_leave_history(
        payload["channel"],
        payload["text"],
        payload.get("cancellable_requests", []),
    )


def _send_balance_report_menu(db: Session, job: DurableJob, payload: dict) -> None:
    RealSlackClient().send_balance_report_menu(
        payload["channel"],
        payload["text"],
        payload.get("can_manage", False),
    )


def _send_balance_report_page(db: Session, job: DurableJob, payload: dict) -> None:
    from app.api.routes import _balance_report_page_result, _sync_policy_from_db

    if "text" not in payload:
        requester = db.get(Employee, payload["requester_id"])
        if requester is None:
            raise PermanentJobError("Balance report requester no longer exists")
        _sync_policy_from_db(db)
        result = _balance_report_page_result(
            db,
            requester,
            payload.get("department"),
            int(payload.get("page", 0)),
        )
    else:
        result = {
            "reply": payload["text"],
            "department": payload.get("department"),
            "page": payload["page"],
            "total_pages": payload["total_pages"],
        }
    RealSlackClient().send_balance_report_page(
        payload["channel"],
        result["reply"],
        result["department"],
        result["page"],
        result["total_pages"],
    )


def _send_balance_report_csv(db: Session, job: DurableJob, payload: dict) -> None:
    from app.api.routes import _sync_policy_from_db, _visible_employee_query
    from app.services.balances import BalanceService
    from app.services.policy import leave_policy

    requester = db.get(Employee, payload["requester_id"])
    if requester is None or requester.role not in {"manager", "hr", "admin"}:
        raise PermanentJobError("Balance report requester is not authorized")
    _sync_policy_from_db(db)
    employees = db.scalars(_visible_employee_query(requester).order_by(Employee.name)).all()
    year = date.today().year
    balance_service = BalanceService(db)
    grouped = balance_service.get_taken_days_for_employees(
        [employee.id for employee in employees],
        year,
    )
    allocations = balance_service.get_allocated_days_for_employees(
        [employee.id for employee in employees],
        year,
    )
    eligible = balance_service.get_eligible_leave_types_for_employees(
        [employee.id for employee in employees],
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        ["employee", "email", "department", "leave_type", "allocated_days", "used_days", "remaining_days", "year"]
    )
    for employee in employees:
        for leave_type in sorted(eligible[employee.id]):
            rule = leave_policy.get(leave_type)
            used = grouped.get(employee.id, {}).get(leave_type, 0.0)
            allocated = allocations[employee.id][leave_type]
            writer.writerow(
                [
                    employee.name,
                    employee.email,
                    employee.department or "",
                    rule.display_name,
                    allocated,
                    used,
                    allocated - used,
                    year,
                ]
            )
    try:
        RealSlackClient().upload_csv(
            payload["channel"],
            f"employee-leave-balances-{year}.csv",
            output.getvalue().encode("utf-8-sig"),
            f"Employee leave balances for {year}",
        )
    except RuntimeError as exc:
        if "missing_scope" not in str(exc):
            raise
        _queue_message(
            db,
            f"balance-csv-scope:{job.id}",
            payload["channel"],
            "CSV export needs the Slack `files:write` permission. Ask the app administrator to add it and reinstall the app.",
        )


def _publish_employee_home(db: Session, job: DurableJob, payload: dict) -> None:
    RealSlackClient().publish_employee_home(payload["slack_user_id"])


def _send_approval_card(db: Session, job: DurableJob, payload: dict) -> None:
    request = db.scalar(
        select(LeaveRequest)
        .where(LeaveRequest.id == payload["leave_request_id"])
        .with_for_update()
    )
    if request is None:
        raise PermanentJobError("Leave request no longer exists")
    response = RealSlackClient().send_leave_approval(
        payload["recipient_slack_user_id"],
        request.id,
        request.employee.name,
        request.leave_type,
        str(request.start_date),
        str(request.end_date),
        float(request.days_requested),
        request.document_key,
        request.reason,
    )
    _store_message_ref(
        request,
        f"approval:{payload['recipient_slack_user_id']}",
        response,
    )


def _send_cancellation_card(db: Session, job: DurableJob, payload: dict) -> None:
    request = db.scalar(
        select(LeaveRequest)
        .where(LeaveRequest.id == payload["leave_request_id"])
        .with_for_update()
    )
    if request is None:
        raise PermanentJobError("Leave request no longer exists")
    response = RealSlackClient().send_cancellation_approval(
        request.employee.manager.slack_user_id,
        request.id,
        request.employee.name,
        request.leave_type,
        str(request.start_date),
        str(request.end_date),
        float(request.days_requested),
    )
    _store_message_ref(
        request,
        f"cancellation:{request.employee.manager.slack_user_id}",
        response,
    )


def _update_leave_cards(db: Session, job: DurableJob, payload: dict) -> None:
    request = db.get(LeaveRequest, payload["leave_request_id"])
    if request is None:
        raise PermanentJobError("Leave request no longer exists")
    refs = _message_refs(request)
    extra_ref = payload.get("extra_ref")
    if extra_ref:
        refs["clicked"] = extra_ref
    prefixes = payload.get("prefixes") or ["approval:", "cancellation:", "clicked"]
    unique_refs = {
        (ref["channel"], ref["ts"])
        for key, ref in refs.items()
        if any(key.startswith(prefix) for prefix in prefixes)
        and ref.get("channel")
        and ref.get("ts")
    }
    slack = RealSlackClient()
    for channel, timestamp in unique_refs:
        slack.update_leave_card(
            channel,
            timestamp,
            request.employee.name,
            request.leave_type,
            str(request.start_date),
            str(request.end_date),
            float(request.days_requested),
            request.document_key,
            request.reason,
            request.status,
        )


def _message_refs(request: LeaveRequest) -> dict:
    try:
        return json.loads(request.slack_message_refs or "{}")
    except json.JSONDecodeError:
        return {}


def _store_message_ref(request: LeaveRequest, key: str, response: dict | None) -> None:
    if not response or not response.get("channel") or not response.get("ts"):
        return
    refs = _message_refs(request)
    refs[key] = {"channel": response["channel"], "ts": response["ts"]}
    request.slack_message_refs = json.dumps(refs)


def _queue_leave_card_update(
    db: Session,
    key: str,
    request: LeaveRequest,
    extra_ref: dict | None = None,
    prefixes: list[str] | None = None,
) -> None:
    enqueue_job(
        db,
        "update_leave_cards",
        key,
        {
            "leave_request_id": request.id,
            "extra_ref": extra_ref,
            "prefixes": prefixes,
        },
    )


def _cancel_leave_request(db: Session, job: DurableJob, payload: dict) -> None:
    request = db.scalar(
        select(LeaveRequest)
        .where(LeaveRequest.id == payload["leave_request_id"])
        .with_for_update()
    )
    employee = db.get(Employee, payload["employee_id"])
    if request is None or employee is None:
        raise PermanentJobError("Employee or leave request no longer exists")
    if request.status not in {
        LeaveRequestStatus.draft.value,
        LeaveRequestStatus.pending_manager.value,
        LeaveRequestStatus.pending_hr.value,
        LeaveRequestStatus.approved.value,
    }:
        _queue_message(
            db,
            f"cancellation-result:{job.id}",
            payload["reply_channel"],
            (
                f"Your {leave_name(request.leave_type)} request is already "
                f"{readable_status(request.status).lower()}."
            ),
        )
        return
    original_status = request.status
    if original_status in {"draft", "pending_manager", "pending_hr"} and request.agentspan_execution_id:
        AgentSpanApprovalWorkflow().cancel(
            request.agentspan_execution_id,
            "Cancelled by employee",
        )
    needs_manager = LeaveRequestService(db).request_cancellation(employee, request)
    db.flush()
    if needs_manager:
        if request.employee.manager is None:
            raise PermanentJobError("Employee has no manager for cancellation approval")
        workflow = AgentSpanApprovalWorkflow()
        workflow.ensure_cancellation_registered()
        handle = workflow.start_cancellation(request.id, job.id)
        request.cancellation_agentspan_execution_id = handle.execution_id
        enqueue_job(
            db,
            "send_cancellation_card",
            f"cancellation-card:{request.id}:{job.id}",
            {"leave_request_id": request.id},
        )
        _queue_leave_card_update(
            db,
            f"approval-card-cancellation-pending:{request.id}:{job.id}",
            request,
            prefixes=["approval:"],
        )
        message = (
            f"Your manager has been asked to approve cancellation of your "
            f"{leave_name(request.leave_type)} request."
        )
    else:
        _queue_external_update(db, request, employee)
        _queue_leave_card_update(
            db,
            f"approval-card-cancelled:{request.id}",
            request,
            prefixes=["approval:"],
        )
        message = f"Your {leave_name(request.leave_type)} request has been cancelled."
    _queue_message(
        db,
        f"cancellation-result:{job.id}",
        payload["reply_channel"],
        message,
    )


def _decide_leave_cancellation(db: Session, job: DurableJob, payload: dict) -> None:
    request = db.scalar(
        select(LeaveRequest)
        .where(LeaveRequest.id == payload["leave_request_id"])
        .with_for_update()
    )
    approver = db.get(Employee, payload["approver_id"])
    if request is None or approver is None:
        raise PermanentJobError("Approver or leave request no longer exists")
    if request.status != LeaveRequestStatus.pending_cancellation_manager.value:
        _queue_leave_card_update(
            db,
            f"cancellation-card-stale:{job.id}",
            request,
            payload.get("message_ref"),
        )
        _queue_message(
            db,
            f"cancellation-manager-result:{job.id}",
            payload["reply_channel"],
            (
                f"*{request.employee.name}'s {leave_name(request.leave_type)} request* "
                f"is already {readable_status(request.status).lower()}."
            ),
        )
        return
    if not request.cancellation_agentspan_execution_id:
        raise PermanentJobError("Cancellation workflow has not started")
    AgentSpanApprovalWorkflow().decide_cancellation(
        request.cancellation_agentspan_execution_id,
        payload["approved"],
    )
    LeaveRequestService(db).record_cancellation_decision(
        approver,
        request,
        payload["approved"],
    )
    db.flush()
    _queue_external_update(db, request, approver)
    _queue_leave_card_update(
        db,
        f"cancellation-card-update:{job.id}",
        request,
        payload.get("message_ref"),
    )
    outcome = "cancelled" if payload["approved"] else "kept approved"
    _queue_message(
        db,
        f"cancellation-manager-result:{job.id}",
        payload["reply_channel"],
        f"*{request.employee.name}'s {leave_name(request.leave_type)} request* has been {outcome}.",
    )
    _queue_message(
        db,
        f"cancellation-employee-result:{job.id}",
        request.employee.slack_user_id,
        f"Your {leave_name(request.leave_type)} request has been {outcome}.",
    )


def _adjust_leave_balance(db: Session, job: DurableJob, payload: dict) -> None:
    from app.api.routes import _sync_policy_from_db
    from app.services.balances import BalanceService

    _sync_policy_from_db(db)
    adjuster = db.get(Employee, payload["adjuster_id"])
    employee = db.get(Employee, payload["employee_id"])
    if adjuster is None or employee is None:
        raise PermanentJobError("Adjuster or employee no longer exists")
    service = BalanceService(db)
    try:
        leave_type = payload["leave_type"]
        year = int(payload["year"])
        days_delta = float(payload["days_delta"])
        if settings.performance_api_mode.lower() == "live":
            from app.services.policy import leave_policy

            rule = leave_policy.get(leave_type)
            client = PerformanceAPIClient()
            balance = client.find_balance(
                employee.email,
                leave_type,
                rule.display_name,
            )
            if balance is None:
                raise ValueError(
                    f"{employee.name} is not eligible for {rule.display_name}."
                )
            target_remaining = float(
                payload.get(
                    "external_target_balance",
                    float(balance.get("balance") or 0) + days_delta,
                )
            )
            if target_remaining < 0 and not rule.allow_negative_balance:
                raise ValueError(
                    "This adjustment would make the remaining balance negative."
                )
            client.update_balance(str(balance["id"]), target_remaining)
            service.record_adjustment(
                adjuster,
                employee,
                leave_type,
                year,
                days_delta,
                payload["reason"],
            )
        else:
            service.adjust_allocation(
                adjuster,
                employee,
                leave_type,
                year,
                days_delta,
                payload["reason"],
            )
    except ValueError as exc:
        _queue_message(
            db,
            f"balance-adjustment-error:{job.id}",
            payload["reply_channel"],
            str(exc),
        )
        return
    allocated = service.get_allocated_days(
        employee.id,
        payload["leave_type"],
        int(payload["year"]),
    )
    used = service.get_taken_days(
        employee.id,
        payload["leave_type"],
        int(payload["year"]),
    )
    _queue_message(
        db,
        f"balance-adjustment-result:{job.id}",
        payload["reply_channel"],
        (
            f"*{employee.name}'s {leave_name(payload['leave_type'])} balance was adjusted.*\n"
            f"*Allocated:* {allocated:g} days\n"
            f"*Used:* {used:g} days\n"
            f"*Remaining:* {allocated - used:g} days"
        ),
    )


def _override_leave_request(db: Session, job: DurableJob, payload: dict) -> None:
    request = db.scalar(
        select(LeaveRequest)
        .where(LeaveRequest.id == payload["leave_request_id"])
        .with_for_update()
    )
    approver = db.get(Employee, payload["approver_id"])
    if request is None or approver is None:
        raise PermanentJobError("Approver or leave request no longer exists")
    original_status = request.status
    try:
        LeaveRequestService(db).override_request(
            approver,
            request,
            payload["status"],
            payload["reason"],
        )
    except ValueError as exc:
        _queue_message(
            db,
            f"request-override-error:{job.id}",
            payload["reply_channel"],
            str(exc),
        )
        return
    if request.agentspan_execution_id and original_status in {
        "pending_manager",
        "pending_hr",
    }:
        AgentSpanApprovalWorkflow().cancel(
            request.agentspan_execution_id,
            f"HR override: {payload['reason']}",
        )
    if (
        request.cancellation_agentspan_execution_id
        and original_status == LeaveRequestStatus.pending_cancellation_manager.value
    ):
        AgentSpanApprovalWorkflow().cancel(
            request.cancellation_agentspan_execution_id,
            f"HR override: {payload['reason']}",
        )
    db.flush()
    _queue_external_update(db, request, approver)
    _queue_leave_card_update(
        db,
        f"override-card-update:{job.id}",
        request,
    )
    _queue_message(
        db,
        f"override-result:{job.id}",
        payload["reply_channel"],
        (
            f"*{request.employee.name}'s {leave_name(request.leave_type)} request* "
            f"was overridden to *{request.status}*."
        ),
    )
    _queue_message(
        db,
        f"override-employee-result:{job.id}",
        request.employee.slack_user_id,
        (
            f"HR changed your {leave_name(request.leave_type)} request to "
            f"*{request.status}*.\n*Reason:* {payload['reason']}"
        ),
    )


def _upload_leave_document(db: Session, job: DurableJob, payload: dict) -> None:
    request = db.scalar(
        select(LeaveRequest).where(LeaveRequest.id == payload["leave_request_id"]).with_for_update()
    )
    if request is None:
        raise PermanentJobError("Leave request no longer exists")
    if request.document_key and request.document_key.startswith("https://"):
        return
    if not request.document_key or not request.document_key.startswith("slack:"):
        raise PermanentJobError("Leave request has no Slack document")

    try:
        filename, content_type, content = RealSlackClient().download_file(
            request.document_key.removeprefix("slack:")
        )
        document_url = AutochekDocumentStorage().store_bytes(filename, content, content_type)
    except ValueError as exc:
        request.status = "cancelled"
        request.document_key = None
        _queue_message(
            db,
            f"document-rejected:{request.id}",
            request.employee.slack_user_id,
            f"Your {leave_name(request.leave_type)} request was not submitted: {exc}",
        )
        return

    request.document_key = document_url
    request.status = "pending_manager"
    enqueue_job(
        db,
        (
            "create_external_leave_request"
            if settings.performance_api_mode.lower() == "live"
            else "start_agentspan"
        ),
        f"agentspan-start:leave-request:{request.id}",
        {"leave_request_id": request.id},
    )
    _queue_message(
        db,
        f"document-uploaded:{request.id}",
        request.employee.slack_user_id,
        (
            f"Your supporting document was uploaded. "
            f"Your {leave_name(request.leave_type)} request will now be sent to your manager."
        ),
    )


def _queue_message(db: Session, key: str, channel: str, text: str) -> None:
    enqueue_job(db, "send_slack_message", key, {"channel": channel, "text": text})
