import csv
from datetime import date
import io
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.slack import RealSlackClient
from app.adapters.storage import AutochekDocumentStorage
from app.adapters.workflow import AgentSpanApprovalWorkflow
from app.db.models import DurableJob, Employee, LeaveRequest
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
        "decide_agentspan": _decide_agentspan,
        "send_slack_message": _send_slack_message,
        "send_leave_request_prompt": _send_leave_request_prompt,
        "send_employee_menu": _send_employee_menu,
        "send_balance_report_menu": _send_balance_report_menu,
        "send_balance_report_page": _send_balance_report_page,
        "send_balance_report_csv": _send_balance_report_csv,
        "publish_employee_home": _publish_employee_home,
        "send_approval_card": _send_approval_card,
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
    if result.get("type") == "balance_report_menu":
        enqueue_job(
            db,
            "send_balance_report_menu",
            f"chat-reply:{source_job.id}",
            {"channel": reply_channel, "text": result["reply"]},
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
            },
        )


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


def _decide_agentspan(db: Session, job: DurableJob, payload: dict) -> None:
    request = db.scalar(
        select(LeaveRequest).where(LeaveRequest.id == payload["leave_request_id"]).with_for_update()
    )
    approver = db.get(Employee, payload["approver_id"])
    if request is None or approver is None:
        raise PermanentJobError("Approver or leave request no longer exists")

    expected_status = "pending_manager" if payload["stage"] == "manager" else "pending_hr"
    if request.status != expected_status:
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


def _send_balance_report_menu(db: Session, job: DurableJob, payload: dict) -> None:
    RealSlackClient().send_balance_report_menu(payload["channel"], payload["text"])


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
    grouped = BalanceService(db).get_taken_days_for_employees(
        [employee.id for employee in employees],
        year,
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        ["employee", "email", "department", "leave_type", "allocated_days", "used_days", "remaining_days", "year"]
    )
    for employee in employees:
        for leave_type, rule in leave_policy.all().items():
            used = grouped.get(employee.id, {}).get(leave_type, 0.0)
            writer.writerow(
                [
                    employee.name,
                    employee.email,
                    employee.department or "",
                    rule.display_name,
                    rule.annual_days,
                    used,
                    rule.annual_days - used,
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
    request = db.get(LeaveRequest, payload["leave_request_id"])
    if request is None:
        raise PermanentJobError("Leave request no longer exists")
    RealSlackClient().send_leave_approval(
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
        "start_agentspan",
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
