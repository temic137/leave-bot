from datetime import date
import hashlib
import hmac
import json
import time

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.workflow import WorkflowHandle
from app.api import routes
from app.db.models import ApprovalEvent, Base, DurableJob, Employee, LeaveRequest
from app.db.session import get_db
from app.main import app
from app.services.jobs import DurableJobWorker, enqueue_job, utc_now


def make_session_factory(database_url: str):
    engine = create_engine(database_url, connect_args={"check_same_thread": False}, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def sign_slack(body: bytes, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
    }


def test_slack_event_is_acknowledged_and_deduplicated_before_processing(tmp_path, monkeypatch) -> None:
    _engine, factory = make_session_factory(f"sqlite:///{tmp_path / 'webhook.db'}")

    def override_db():
        with factory() as db:
            yield db

    secret = "test-secret"
    monkeypatch.setattr(routes.settings, "slack_signing_secret", secret)
    monkeypatch.setattr("app.main.settings.job_worker_enabled", False)
    monkeypatch.setattr(
        "app.adapters.slack.RealSlackClient.send_channel_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Slack must not be called in webhook")),
    )
    app.dependency_overrides[get_db] = override_db
    payload = {
        "type": "event_callback",
        "event_id": "Ev-duplicate",
        "event": {"type": "message", "user": "U_EMPLOYEE", "channel": "D_TEST", "text": "show my balance"},
    }
    body = json.dumps(payload).encode()
    try:
        with TestClient(app) as client:
            first = client.post("/slack/events", content=body, headers=sign_slack(body, secret))
            second = client.post("/slack/events", content=body, headers=sign_slack(body, secret))
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == second.status_code == 200
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(DurableJob)) == 1
        job = db.scalar(select(DurableJob))
        assert job.status == "pending"
        assert job.idempotency_key == "slack-event:Ev-duplicate"


def test_pending_job_survives_process_restart(tmp_path) -> None:
    path = tmp_path / "restart.db"
    first_engine, first_factory = make_session_factory(f"sqlite:///{path}")
    with first_factory() as db:
        enqueue_job(db, "test", "restart-job", {"value": 42})
        db.commit()
    first_engine.dispose()

    second_engine, second_factory = make_session_factory(f"sqlite:///{path}")
    handled = []
    worker = DurableJobWorker(second_factory, lambda db, job: handled.append(json.loads(job.payload_json)), retry_base_seconds=0)
    assert worker.run_once()
    with second_factory() as db:
        assert db.scalar(select(DurableJob.status)) == "succeeded"
    assert handled == [{"value": 42}]
    second_engine.dispose()


def test_slack_failure_is_retried(tmp_path, monkeypatch) -> None:
    _engine, factory = make_session_factory(f"sqlite:///{tmp_path / 'slack-retry.db'}")
    calls = []

    def flaky_send(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("Slack unavailable")

    monkeypatch.setattr("app.adapters.slack.RealSlackClient.send_channel_message", flaky_send)
    with factory() as db:
        enqueue_job(db, "send_slack_message", "send-once", {"channel": "D1", "text": "hello"})
        db.commit()
    worker = DurableJobWorker(factory, retry_base_seconds=0)
    worker.run_once()
    with factory() as db:
        job = db.scalar(select(DurableJob))
        assert job.status == "pending"
        assert job.attempts == 1
    worker.run_once()
    with factory() as db:
        job = db.scalar(select(DurableJob))
        assert job.status == "succeeded"
        assert job.attempts == 2


def test_agentspan_failure_is_retried_without_losing_request(tmp_path, monkeypatch) -> None:
    _engine, factory = make_session_factory(f"sqlite:///{tmp_path / 'agentspan-retry.db'}")
    calls = []

    def flaky_start(self, leave_request_id: int, requires_hr: bool):
        calls.append(leave_request_id)
        if len(calls) == 1:
            raise httpx.ConnectError("AgentSpan unavailable")
        return WorkflowHandle("workflow-123")

    monkeypatch.setattr("app.services.job_handlers.AgentSpanApprovalWorkflow.start", flaky_start)
    monkeypatch.setattr("app.services.job_handlers.AgentSpanApprovalWorkflow.ensure_registered", lambda *args: None)
    with factory() as db:
        manager = Employee(slack_user_id="U_MANAGER", email="manager@example.com", name="Manager", role="manager")
        employee = Employee(slack_user_id="U_EMPLOYEE", email="employee@example.com", name="Employee", manager=manager)
        db.add_all([manager, employee])
        db.flush()
        request = LeaveRequest(
            employee_id=employee.id,
            leave_type="annual",
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 20),
            days_requested=1,
            status="pending_manager",
        )
        db.add(request)
        db.flush()
        request_id = request.id
        enqueue_job(db, "start_agentspan", f"start:{request_id}", {"leave_request_id": request_id})
        db.commit()

    worker = DurableJobWorker(factory, retry_base_seconds=0)
    worker.run_once()
    with factory() as db:
        assert db.get(LeaveRequest, request_id).agentspan_execution_id is None
        assert db.scalar(select(DurableJob).where(DurableJob.job_type == "start_agentspan")).status == "pending"
    worker.run_once()
    with factory() as db:
        assert db.get(LeaveRequest, request_id).agentspan_execution_id == "workflow-123"
        assert db.scalar(select(DurableJob).where(DurableJob.job_type == "start_agentspan")).status == "succeeded"
        assert db.scalar(select(func.count()).select_from(DurableJob).where(DurableJob.job_type == "send_approval_card")) == 1


def test_groq_failure_uses_deterministic_parser(monkeypatch) -> None:
    monkeypatch.setattr(routes.settings, "groq_api_key", "configured-for-test")
    monkeypatch.setattr(
        routes.GroqMessageParser,
        "parse",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("Groq unavailable")),
    )
    parsed = routes._parse_leave_message_from_policy("I need annual leave from 2026-07-20 to 2026-07-21")
    assert parsed.leave_type == "annual"
    assert parsed.start_date == date(2026, 7, 20)
    assert parsed.end_date == date(2026, 7, 21)
    assert not parsed.missing_fields


def test_database_pool_reconnects_after_dispose(tmp_path) -> None:
    engine, factory = make_session_factory(f"sqlite:///{tmp_path / 'reconnect.db'}")
    with factory() as db:
        assert db.scalar(text("SELECT 1")) == 1
    engine.dispose()
    with factory() as db:
        assert db.scalar(text("SELECT 1")) == 1


def test_stale_processing_job_is_recovered(tmp_path) -> None:
    _engine, factory = make_session_factory(f"sqlite:///{tmp_path / 'stale.db'}")
    with factory() as db:
        job = enqueue_job(db, "test", "stale", {})
        job.status = "processing"
        job.locked_at = utc_now().replace(year=2020)
        db.commit()
    worker = DurableJobWorker(factory, lambda db, job: None, retry_base_seconds=0)
    worker.run_once()
    with factory() as db:
        assert db.scalar(select(DurableJob.status)) == "succeeded"


def test_leave_submission_runs_end_to_end_through_queue(tmp_path, monkeypatch) -> None:
    _engine, factory = make_session_factory(f"sqlite:///{tmp_path / 'end-to-end.db'}")
    sent_messages = []
    sent_cards = []
    monkeypatch.setattr(routes.settings, "groq_api_key", "")
    monkeypatch.setattr(
        "app.services.job_handlers.AgentSpanApprovalWorkflow.start",
        lambda self, leave_request_id, requires_hr: WorkflowHandle("workflow-e2e"),
    )
    monkeypatch.setattr("app.services.job_handlers.AgentSpanApprovalWorkflow.ensure_registered", lambda *args: None)
    monkeypatch.setattr(
        "app.adapters.slack.RealSlackClient.send_channel_message",
        lambda self, channel, text: sent_messages.append((channel, text)),
    )
    monkeypatch.setattr(
        "app.adapters.slack.RealSlackClient.send_leave_approval",
        lambda self, *args: sent_cards.append(args),
    )
    with factory() as db:
        manager = Employee(slack_user_id="U_MANAGER", email="manager@example.com", name="Manager", role="manager")
        employee = Employee(slack_user_id="U_EMPLOYEE", email="employee@example.com", name="Employee", manager=manager)
        db.add_all([manager, employee])
        payload = {
            "type": "event_callback",
            "event_id": "Ev-e2e",
            "event": {
                "type": "message",
                "user": "U_EMPLOYEE",
                "channel": "D_EMPLOYEE",
                "text": "I need annual leave from 2026-07-20 to 2026-07-21",
            },
        }
        enqueue_job(db, "process_slack_event", "slack-event:Ev-e2e", {"slack_payload": payload})
        db.commit()

    worker = DurableJobWorker(factory, retry_base_seconds=0)
    while worker.run_once():
        pass

    with factory() as db:
        request = db.scalar(select(LeaveRequest))
        assert request.agentspan_execution_id == "workflow-e2e"
        assert request.status == "pending_manager"
        assert db.scalar(select(func.count()).select_from(DurableJob).where(DurableJob.status != "succeeded")) == 0
    assert sent_messages[0][0] == "D_EMPLOYEE"
    assert "has been recorded" in sent_messages[0][1]
    assert sent_cards[0][0] == "U_MANAGER"


def test_duplicate_approval_jobs_record_one_decision(tmp_path, monkeypatch) -> None:
    _engine, factory = make_session_factory(f"sqlite:///{tmp_path / 'approval.db'}")
    decisions = []
    messages = []
    monkeypatch.setattr(
        "app.services.job_handlers.AgentSpanApprovalWorkflow.decide",
        lambda self, execution_id, approved, reason, stage: decisions.append((execution_id, stage)),
    )
    monkeypatch.setattr(
        "app.adapters.slack.RealSlackClient.send_channel_message",
        lambda self, channel, message: messages.append((channel, message)),
    )
    with factory() as db:
        manager = Employee(slack_user_id="U_MANAGER", email="manager@example.com", name="Manager", role="manager")
        employee = Employee(slack_user_id="U_EMPLOYEE", email="employee@example.com", name="Employee", manager=manager)
        db.add_all([manager, employee])
        db.flush()
        request = LeaveRequest(
            employee_id=employee.id,
            leave_type="annual",
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 20),
            days_requested=1,
            status="pending_manager",
            agentspan_execution_id="workflow-approval",
        )
        db.add(request)
        db.flush()
        job_payload = {
            "leave_request_id": request.id,
            "approver_id": manager.id,
            "approved": True,
            "stage": "manager",
            "reply_channel": manager.slack_user_id,
        }
        enqueue_job(db, "decide_agentspan", "decision:first", job_payload)
        enqueue_job(db, "decide_agentspan", "decision:duplicate", job_payload)
        db.commit()

    worker = DurableJobWorker(factory, retry_base_seconds=0)
    while worker.run_once():
        pass

    with factory() as db:
        assert db.scalar(select(LeaveRequest.status)) == "approved"
        assert db.scalar(select(func.count()).select_from(ApprovalEvent)) == 1
    assert decisions == [("workflow-approval", "manager")]
    assert any("already approved" in message for _, message in messages)
