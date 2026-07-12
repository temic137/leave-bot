from datetime import date
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import routes
from app.main import app
from app.adapters.slack import RealSlackClient
from app.adapters.workflow import AgentSpanApprovalWorkflow
from app.db.models import Employee, LeavePolicyVersion, LeaveRequestStatus
from app.db.session import Base
from app.schemas.leave import LeaveRequestCreate
from app.services.balances import BalanceService
from app.services.leave_requests import LeaveRequestService
from app.services.permissions import can_approve_request, can_view_balance
from app.services.policy import LeavePolicy


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def seed_people(db: Session) -> tuple[Employee, Employee, Employee]:
    manager = Employee(slack_user_id="U_MANAGER", email="manager@example.com", name="Manager", role="manager")
    employee = Employee(
        slack_user_id="U_EMPLOYEE",
        email="employee@example.com",
        name="Employee",
        role="employee",
        manager=manager,
    )
    hr = Employee(slack_user_id="U_HR", email="hr@example.com", name="HR", role="hr")
    db.add_all([manager, employee, hr])
    db.flush()
    return employee, manager, hr


def test_database_schema_has_only_the_five_core_tables() -> None:
    assert set(Base.metadata.tables) == {
        "employees",
        "leave_requests",
        "approval_events",
        "leave_policy_versions",
        "conversation_sessions",
    }


def test_manager_can_view_direct_report_balance(db: Session) -> None:
    employee, manager, hr = seed_people(db)

    assert can_view_balance(employee, employee)
    assert can_view_balance(manager, employee)
    assert can_view_balance(hr, employee)
    assert not can_view_balance(employee, manager)


def test_manager_approval_deducts_balance_for_manager_only_leave(db: Session) -> None:
    employee, manager, _hr = seed_people(db)
    balances = BalanceService(db)
    balances.initialize_default_balances(employee.id, 2026)
    request = LeaveRequestService(db).create_request(
        LeaveRequestCreate(
            employee_id=employee.id,
            leave_type="annual",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 8),
            reason="family event",
        )
    )

    assert can_approve_request(manager, request)
    LeaveRequestService(db).record_manager_decision(manager, request, approved=True)
    db.flush()

    assert request.status == LeaveRequestStatus.approved.value
    assert balances.get_taken_days(employee.id, "annual", 2026) == 3.0


def test_hr_required_leave_waits_after_manager_approval(db: Session) -> None:
    employee, manager, hr = seed_people(db)
    balances = BalanceService(db)
    balances.initialize_default_balances(employee.id, 2026)
    request = LeaveRequestService(db).create_request(
        LeaveRequestCreate(
            employee_id=employee.id,
            leave_type="maternity",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 3),
            reason="parental leave",
            document_key="doc-key",
        )
    )

    LeaveRequestService(db).record_manager_decision(manager, request, approved=True)
    db.flush()
    assert request.status == LeaveRequestStatus.pending_hr.value
    assert balances.get_taken_days(employee.id, "maternity", 2026) == 0.0

    assert can_approve_request(hr, request)
    LeaveRequestService(db).record_hr_decision(hr, request, approved=True)
    db.flush()

    assert request.status == LeaveRequestStatus.approved.value
    assert balances.get_taken_days(employee.id, "maternity", 2026) == 3.0


def test_document_required_leave_rejects_missing_document(db: Session) -> None:
    employee, _manager, _hr = seed_people(db)
    BalanceService(db).initialize_default_balances(employee.id, 2026)

    with pytest.raises(ValueError, match="requires a document"):
        LeaveRequestService(db).create_request(
            LeaveRequestCreate(
                employee_id=employee.id,
                leave_type="sick",
                start_date=date(2026, 7, 6),
                end_date=date(2026, 7, 6),
                reason="illness",
            )
        )


def test_admin_can_add_leave_type_policy(tmp_path, db: Session) -> None:
    employee, _manager, _hr = seed_people(db)
    policy_path = tmp_path / "leave_policy.json"
    policy_path.write_text(
        """{
  "annual": {
    "display_name": "Annual Leave",
    "annual_days": 20,
    "requires_document": false,
    "requires_hr": false,
    "allow_negative_balance": false
  }
}
""",
        encoding="utf-8",
    )
    policy = LeavePolicy(policy_path)

    rule = policy.upsert(
        key="Study Leave",
        display_name="Study Leave",
        annual_days=5,
        requires_document=True,
        requires_hr=True,
        allow_negative_balance=False,
    )
    BalanceService(db, policy).initialize_default_balances_for_leave_type(rule.key, rule.annual_days, 2026)
    db.flush()

    assert rule.key == "study_leave"
    assert policy.get("study_leave").requires_hr
    assert BalanceService(db, policy).get_taken_days(employee.id, "study_leave", 2026) == 0.0


def test_agentspan_workflow_starts_idempotent_execution(monkeypatch) -> None:
    calls = []

    class Response:
        text = "workflow-123"

        def raise_for_status(self):
            return None

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr("app.adapters.workflow.httpx.request", request)
    handle = AgentSpanApprovalWorkflow("http://agentspan:6767").start(42, requires_hr=True)

    assert handle.execution_id == "workflow-123"
    assert calls[0][1].endswith("/api/workflow/leave_approval_manager_hr_v1")
    assert calls[0][2]["params"]["correlationId"] == "leave-request-42"
    assert calls[0][2]["json"] == {"leave_request_id": 42}


@pytest.mark.parametrize("approved", [True, False])
def test_agentspan_workflow_forwards_human_decision(monkeypatch, approved: bool) -> None:
    calls = []

    class Response:
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "tasks": [
                    {"taskType": "HUMAN", "status": "IN_PROGRESS", "taskId": "task-1"}
                ]
            }

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr("app.adapters.workflow.httpx.request", request)
    AgentSpanApprovalWorkflow("http://agentspan:6767").decide("workflow-123", approved, "not allowed")

    if approved:
        assert [call[0] for call in calls] == ["GET", "POST"]
        assert calls[1][2]["json"]["taskId"] == "task-1"
    else:
        assert [call[0] for call in calls] == ["DELETE"]
        assert calls[0][2]["params"]["reason"] == "not allowed"


def test_admin_can_edit_policy_as_plain_text(tmp_path) -> None:
    policy_path = tmp_path / "leave_policy.json"
    policy_path.write_text("{}", encoding="utf-8")
    policy = LeavePolicy(policy_path)

    rules = policy.replace_raw_text(
        """
Annual Leave: 20 days maximum. No document required. Manager approval only.
Sick Leave: 10 days maximum. Document required. Manager approval only.
Maternity Leave: 90 days maximum. Document required. HR approval required. Negative balance allowed.
"""
    )

    assert rules["annual_leave"].annual_days == 20
    assert not rules["annual_leave"].requires_document
    assert rules["sick_leave"].requires_document
    assert not rules["sick_leave"].requires_hr
    assert rules["maternity_leave"].requires_hr
    assert rules["maternity_leave"].allow_negative_balance
    assert "Sick Leave: 10 days maximum." in policy.to_raw_text()


def test_slack_url_verification_checks_signature() -> None:
    routes.settings.slack_signing_secret = "test-secret"
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode("utf-8")
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    signature = "v0=" + hmac.new(b"test-secret", base, hashlib.sha256).hexdigest()

    response = TestClient(app).post(
        "/slack/events",
        content=body,
        headers={
            "content-type": "application/json",
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": signature,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "abc123"}


def test_database_policy_version_restores_active_policy(tmp_path, db: Session, monkeypatch) -> None:
    policy_path = tmp_path / "leave_policy.json"
    policy_path.write_text("{}", encoding="utf-8")
    policy = LeavePolicy(policy_path)
    monkeypatch.setattr(routes, "leave_policy", policy)
    db.add(
        LeavePolicyVersion(
            version=2,
            raw_text="Study Leave: 5 days maximum. Document required. HR approval required.\n",
            rules_json="{}",
        )
    )
    db.commit()

    version = routes._sync_policy_from_db(db)

    assert version.version == 2
    assert policy.get("study_leave").requires_document
    assert policy.get("study_leave").requires_hr


def test_slack_approval_message_contains_buttons(monkeypatch) -> None:
    sent = {}
    client = RealSlackClient(token="test-token")

    def capture(method: str, payload: dict) -> dict:
        sent.update({"method": method, "payload": payload})
        return {"ok": True}

    monkeypatch.setattr(client, "_api", capture)
    client.send_leave_approval("U_MANAGER", 42, "Temi", "annual", "2026-07-15", "2026-07-16", 2)

    actions = sent["payload"]["blocks"][1]["elements"]
    assert sent["method"] == "chat.postMessage"
    assert [(action["action_id"], action["value"]) for action in actions] == [
        ("approve_leave", "42"),
        ("reject_leave", "42"),
    ]
