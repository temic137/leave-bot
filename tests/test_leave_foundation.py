from datetime import date
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api import routes
from app.main import app
from app.adapters.slack import RealSlackClient
from app.adapters.storage import validate_document
from app.adapters.workflow import AgentSpanApprovalWorkflow
from app.db.models import (
    ApprovalEvent,
    DurableJob,
    Employee,
    LeaveBalanceAdjustment,
    LeavePolicyVersion,
    LeaveRequest,
    LeaveRequestStatus,
)
from app.db.session import Base
from app.schemas.leave import LeaveRequestCreate
from app.services.balances import BalanceService
from app.services.dates import calculate_leave_days
from app.services.employee_sync import EmployeeSyncService
from app.services.leave_requests import LeaveRequestService
from app.services.intents import IntentRouter
from app.services import job_handlers
from app.services.permissions import can_approve_request, can_view_balance
from app.services.policy import LeavePolicy, leave_policy


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


def test_database_schema_has_six_business_tables_and_one_job_table() -> None:
    assert set(Base.metadata.tables) == {
        "employees",
        "leave_requests",
        "leave_balance_adjustments",
        "approval_events",
        "leave_policy_versions",
        "durable_jobs",
    }


def test_manager_can_view_direct_report_balance(db: Session) -> None:
    employee, manager, hr = seed_people(db)

    assert can_view_balance(employee, employee)
    assert can_view_balance(manager, employee)
    assert can_view_balance(hr, employee)
    assert not can_view_balance(employee, manager)


def test_slack_sync_refreshes_placeholder_email_without_losing_manager(db: Session) -> None:
    employee, manager, _hr = seed_people(db)
    employee.email = "placeholder@test.invalid"
    db.flush()

    synced = EmployeeSyncService(db).upsert_slack_user(
        employee.slack_user_id,
        "employee@company.example",
        "Updated Employee",
    )

    assert synced.id == employee.id
    assert synced.email == "employee@company.example"
    assert synced.name == "Updated Employee"
    assert synced.manager_id == manager.id


def test_slack_sync_keeps_same_email_users_from_different_workspaces_separate(db: Session) -> None:
    original = Employee(
        slack_user_id="U_COMPANY",
        email="employee@company.example",
        name="Company Employee",
    )
    db.add(original)
    db.flush()

    test_user = EmployeeSyncService(db).upsert_slack_user(
        "U_TEST",
        "employee@company.example",
        "Test Employee",
    )

    assert test_user.id != original.id
    assert test_user.email == "u_test@slack-id.invalid"
    assert original.slack_user_id == "U_COMPANY"
    assert original.email == "employee@company.example"


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
    assert balances.get_taken_days(employee.id, "maternity", 2026) == 1.0


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
    summary = sent["payload"]["blocks"][0]["text"]["text"]
    assert "*Leave request from Temi*" in summary
    assert "*Leave type:* Annual Leave" in summary
    assert "*Dates:* 15 July 2026 to 16 July 2026" in summary
    assert "Request #42" not in summary
    assert [(action["action_id"], action["value"]) for action in actions] == [
        ("approve_leave", "42"),
        ("reject_leave", "42"),
    ]


def test_intent_router_uses_confidence_and_margin() -> None:
    vectors = {
        "request example": [1.0, 0.0],
        "balance example": [0.0, 1.0],
        "request message": [0.98, 0.02],
        "unclear message": [0.7, 0.7],
    }

    class FakeEmbedding:
        def embed(self, texts):
            return (vectors[text] for text in texts)

    router = IntentRouter(
        model=FakeEmbedding(),
        examples={
            "request_leave": ["request example"],
            "check_balance": ["balance example"],
        },
        threshold=0.5,
        margin_threshold=0.1,
    )

    assert router.classify("request message").intent == "request_leave"
    assert router.classify("unclear message").intent == "unknown"


def test_leave_request_button_explains_what_it_opens(monkeypatch) -> None:
    sent = {}
    client = RealSlackClient(token="test-token")

    def capture(method: str, payload: dict) -> dict:
        sent.update({"method": method, "payload": payload})
        return {"ok": True}

    monkeypatch.setattr(client, "_api", capture)
    explanation = "Click Request leave to open a form for your leave type and dates."
    client.send_leave_request_prompt("D_EMPLOYEE", explanation)

    assert sent["payload"]["blocks"][0]["text"]["text"] == explanation
    button = sent["payload"]["blocks"][1]["elements"][0]
    assert button["action_id"] == "open_leave_request_modal"
    assert button["text"]["text"] == "Request leave"


def test_leave_modal_uses_real_file_input(monkeypatch) -> None:
    sent = {}
    client = RealSlackClient(token="test-token")
    monkeypatch.setattr(client, "_api", lambda method, payload: sent.update(payload) or {"ok": True})

    client.open_leave_request_modal("trigger", LeavePolicy().all())

    document = next(block for block in sent["view"]["blocks"] if block.get("block_id") == "document")
    assert document["optional"]
    assert document["element"] == {
        "type": "file_input",
        "action_id": "document_input",
        "filetypes": ["pdf", "jpg", "jpeg", "png"],
        "max_files": 1,
    }


def test_document_validation_checks_type_signature_and_size(monkeypatch) -> None:
    validate_document(b"%PDF-test", "application/pdf")

    with pytest.raises(ValueError, match="reported file type"):
        validate_document(b"not-a-pdf", "application/pdf")
    with pytest.raises(ValueError, match="Only PDF"):
        validate_document(b"GIF89a", "image/gif")
    monkeypatch.setattr("app.adapters.storage.settings.document_max_bytes", 5)
    with pytest.raises(ValueError, match="too large"):
        validate_document(b"%PDF-test", "application/pdf")


def test_slack_file_info_uses_form_encoding(monkeypatch) -> None:
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return type(
            "Response",
            (),
            {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"ok": True, "file": {"id": "F_TEST"}},
            },
        )()

    monkeypatch.setattr("app.adapters.slack.httpx.post", fake_post)

    result = RealSlackClient(token="test-token")._api(
        "files.info",
        {"file": "F_TEST"},
        form_encoded=True,
    )

    assert result["file"]["id"] == "F_TEST"
    assert calls[0][1]["data"] == {"file": "F_TEST"}
    assert "json" not in calls[0][1]


def test_manager_can_ask_for_direct_report_balance(db: Session) -> None:
    employee, manager, _hr = seed_people(db)
    db.add(
        LeaveRequest(
            employee_id=employee.id,
            leave_type="annual",
            start_date=date.today(),
            end_date=date.today(),
            days_requested=2,
            status=LeaveRequestStatus.approved.value,
        )
    )
    db.flush()

    result = routes._balance_result_for_query(db, manager, "show Employee's leave balance")

    assert result["type"] == "balance"
    assert "Leave balance for Employee" in result["reply"]
    assert "*Annual Leave:* 2 used / 20 allocated / 18 remaining" in result["reply"]

    pronoun_result = routes._balance_result_for_query(db, manager, "show me his balance")
    assert pronoun_result["type"] == "balance_report_menu"
    assert "Which employee do you mean?" in pronoun_result["reply"]
    assert "Search employee" in pronoun_result["reply"]


def test_hr_can_view_all_pending_requests(db: Session) -> None:
    employee, _manager, hr = seed_people(db)
    request = LeaveRequest(
        employee_id=employee.id,
        leave_type="annual",
        start_date=date.today(),
        end_date=date.today(),
        days_requested=1,
        status=LeaveRequestStatus.pending_manager.value,
    )
    db.add(request)
    db.flush()

    result = routes._pending_requests_result(db, hr)

    assert result["type"] == "pending_requests"
    assert "*Employee's Annual Leave request*" in result["reply"]
    assert "Waiting for manager approval" in result["reply"]


def test_hr_views_are_limited_to_their_workspace(db: Session) -> None:
    employee, _manager, hr = seed_people(db)
    employee.workspace_id = hr.workspace_id = "T_TEST"
    outsider = Employee(
        workspace_id="T_OTHER",
        slack_user_id="U_OUTSIDER",
        email="outsider@example.com",
        name="Outside Employee",
    )
    db.add(outsider)
    db.flush()
    outside_request = LeaveRequest(
        employee_id=outsider.id,
        leave_type="annual",
        start_date=date.today(),
        end_date=date.today(),
        days_requested=1,
        status=LeaveRequestStatus.pending_hr.value,
    )
    db.add(outside_request)
    db.flush()

    balances = routes._balance_result_for_query(db, hr, "show all employee balances")
    pending = routes._pending_requests_result(db, hr)

    assert "Outside Employee" not in balances["reply"]
    assert "Outside Employee" not in pending["reply"]


def test_leave_days_exclude_weekends() -> None:
    assert calculate_leave_days(date(2026, 7, 10), date(2026, 7, 13)) == 2


def test_request_rejects_overlap_and_insufficient_remaining_days(db: Session) -> None:
    employee, _manager, _hr = seed_people(db)
    db.add(
        LeaveRequest(
            employee_id=employee.id,
            leave_type="annual",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 1),
            days_requested=19,
            status=LeaveRequestStatus.approved.value,
        )
    )
    db.flush()

    with pytest.raises(ValueError, match="only 1 days remain"):
        LeaveRequestService(db).create_request(
            LeaveRequestCreate(
                employee_id=employee.id,
                leave_type="annual",
                start_date=date(2026, 7, 6),
                end_date=date(2026, 7, 7),
            )
        )

    db.add(
        LeaveRequest(
            employee_id=employee.id,
            leave_type="annual",
            start_date=date(2026, 7, 13),
            end_date=date(2026, 7, 15),
            days_requested=3,
            status=LeaveRequestStatus.pending_manager.value,
        )
    )
    db.flush()
    with pytest.raises(ValueError, match="overlap"):
        LeaveRequestService(db).create_request(
            LeaveRequestCreate(
                employee_id=employee.id,
                leave_type="annual",
                start_date=date(2026, 7, 14),
                end_date=date(2026, 7, 14),
            )
        )


def test_large_employee_report_is_compact_and_grouped(db: Session) -> None:
    hr = Employee(
        workspace_id="T_LARGE",
        slack_user_id="U_LARGE_HR",
        email="large.hr@example.com",
        name="Large HR",
        role="hr",
    )
    employees = [
        Employee(
            workspace_id="T_LARGE",
            slack_user_id=f"U_{index:03}",
            email=f"employee{index:03}@example.com",
            name=f"Employee {index:03}",
            department="Operations" if index < 50 else "Sales",
        )
        for index in range(100)
    ]
    db.add_all([hr, *employees])
    db.flush()
    db.add_all(
        [
            LeaveRequest(
                employee_id=employee.id,
                leave_type="annual",
                start_date=date(2026, 7, 6),
                end_date=date(2026, 7, 6),
                days_requested=1,
                status=LeaveRequestStatus.approved.value,
            )
            for employee in employees
        ]
    )
    db.flush()

    menu = routes._balance_result_for_query(db, hr, "show all employee balances")
    assert menu["type"] == "balance_report_menu"
    assert "*Employees:* 100" in menu["reply"]
    assert "Employee 099" not in menu["reply"]

    statements = []
    listener = lambda *args: statements.append(args[2])
    event.listen(db.bind, "before_cursor_execute", listener)
    try:
        grouped = BalanceService(db).get_taken_days_for_employees(
            [employee.id for employee in employees],
            2026,
        )
    finally:
        event.remove(db.bind, "before_cursor_execute", listener)
    assert len(statements) == 1
    assert grouped[employees[0].id]["annual"] == 1

    page = routes._balance_report_page_result(db, hr, "Operations", 0)
    assert page["total_pages"] == 5
    assert "Employee 000" in page["reply"]
    assert "Employee 009" in page["reply"]
    assert "Employee 010" not in page["reply"]
    assert len(page["reply"]) < 3000


def test_balance_report_menu_explains_its_buttons(monkeypatch) -> None:
    sent = {}
    client = RealSlackClient(token="test-token")
    monkeypatch.setattr(
        client,
        "_api",
        lambda method, payload: sent.update({"method": method, "payload": payload}) or {"ok": True},
    )

    client.send_balance_report_menu("U_HR", "Employee report")

    actions = sent["payload"]["blocks"][1]["elements"]
    assert [action["action_id"] for action in actions] == [
        "open_balance_employee_search",
        "open_balance_department_filter",
        "download_balance_report",
    ]


def test_slack_csv_export_uses_external_upload_flow(monkeypatch) -> None:
    calls = []
    client = RealSlackClient(token="test-token")

    def fake_api(method, payload, **kwargs):
        calls.append((method, payload, kwargs))
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D_HR"}}
        if method == "files.getUploadURLExternal":
            return {"upload_url": "https://upload.example", "file_id": "F_REPORT"}
        return {"ok": True}

    monkeypatch.setattr(client, "_api", fake_api)
    monkeypatch.setattr(
        "app.adapters.slack.httpx.post",
        lambda *args, **kwargs: type("Response", (), {"raise_for_status": lambda self: None})(),
    )

    client.upload_csv("U_HR", "report.csv", b"name,used\nAda,2\n", "Leave report")

    assert [call[0] for call in calls] == [
        "conversations.open",
        "files.getUploadURLExternal",
        "files.completeUploadExternal",
    ]
    assert calls[2][1]["channel_id"] == "D_HR"


def test_employee_balance_search_is_workspace_scoped(db: Session) -> None:
    _employee, _manager, hr = seed_people(db)
    hr.workspace_id = "T_TEST"
    teammate = Employee(
        workspace_id="T_TEST",
        slack_user_id="U_ADA",
        email="ada@example.com",
        name="Ada Teammate",
    )
    outsider = Employee(
        workspace_id="T_OTHER",
        slack_user_id="U_ADA_OTHER",
        email="ada.other@example.com",
        name="Ada Outsider",
    )
    db.add_all([teammate, outsider])
    db.flush()

    result = routes._employee_balance_options(
        {
            "action_id": "balance_employee_search",
            "user": {"id": hr.slack_user_id},
            "team": {"id": "T_TEST"},
            "value": "Ada",
        },
        db,
    )

    assert [option["text"]["text"] for option in result["options"]] == ["Ada Teammate"]

    email_result = routes._employee_balance_options(
        {
            "action_id": "balance_employee_search",
            "user": {"id": hr.slack_user_id},
            "team": {"id": "T_TEST"},
            "value": "ada@example.com",
        },
        db,
    )
    assert [option["text"]["text"] for option in email_result["options"]] == ["Ada Teammate"]

    initial_result = routes._employee_balance_options(
        {
            "action_id": "balance_employee_search",
            "user": {"id": hr.slack_user_id},
            "team": {"id": "T_TEST"},
            "value": "",
        },
        db,
    )
    assert "Ada Teammate" in [option["text"]["text"] for option in initial_result["options"]]
    assert "Ada Outsider" not in [option["text"]["text"] for option in initial_result["options"]]


def test_hr_employee_selectors_show_results_without_typing(monkeypatch) -> None:
    views = []
    client = RealSlackClient(token="test-token")
    monkeypatch.setattr(
        client,
        "_api",
        lambda method, payload: views.append(payload["view"]) or {"ok": True},
    )

    client.open_employee_balance_search_modal("trigger-search")
    client.open_balance_adjustment_modal("trigger-adjust", {"annual": leave_policy.get("annual")})

    selectors = [
        view["blocks"][0]["element"]
        for view in views
    ]
    assert all(selector["min_query_length"] == 0 for selector in selectors)


def test_csv_report_contains_only_authorized_employees(db: Session, monkeypatch) -> None:
    employee, _manager, hr = seed_people(db)
    employee.workspace_id = hr.workspace_id = "T_TEST"
    outsider = Employee(
        workspace_id="T_OTHER",
        slack_user_id="U_OUTSIDE_CSV",
        email="outside.csv@example.com",
        name="Outside CSV",
    )
    db.add(outsider)
    db.flush()
    captured = {}
    monkeypatch.setattr(
        "app.adapters.slack.RealSlackClient.upload_csv",
        lambda self, channel, filename, content, title: captured.update(
            {"channel": channel, "filename": filename, "content": content.decode("utf-8-sig")}
        ),
    )
    job = DurableJob(
        id=99,
        job_type="send_balance_report_csv",
        idempotency_key="csv-test",
        payload_json="{}",
    )

    job_handlers._send_balance_report_csv(
        db,
        job,
        {"channel": hr.slack_user_id, "requester_id": hr.id},
    )

    assert captured["channel"] == hr.slack_user_id
    assert "Employee" in captured["content"]
    assert "Annual Leave" in captured["content"]
    assert "Outside CSV" not in captured["content"]


def test_approval_card_update_removes_action_buttons(monkeypatch) -> None:
    sent = {}
    client = RealSlackClient(token="test-token")
    monkeypatch.setattr(
        client,
        "_api",
        lambda method, payload: sent.update({"method": method, "payload": payload}) or {"ok": True},
    )

    client.update_leave_card(
        "D_MANAGER",
        "123.456",
        "Employee",
        "annual",
        "2026-07-06",
        "2026-07-07",
        2,
        None,
        "Family event",
        "approved",
    )

    assert sent["method"] == "chat.update"
    assert sent["payload"]["channel"] == "D_MANAGER"
    assert all(block["type"] != "actions" for block in sent["payload"]["blocks"])
    assert "Approved" in sent["payload"]["blocks"][1]["elements"][0]["text"]


def test_history_message_has_cancellation_buttons(monkeypatch) -> None:
    sent = {}
    client = RealSlackClient(token="test-token")
    monkeypatch.setattr(
        client,
        "_api",
        lambda method, payload: sent.update(payload) or {"ok": True},
    )

    client.send_leave_history(
        "U_EMPLOYEE",
        "History",
        [
            {"id": 1, "status": "pending_manager", "label": "Annual Leave, 6 July"},
            {"id": 2, "status": "approved", "label": "Sick Leave, 8 July"},
        ],
    )

    buttons = [block["elements"][0] for block in sent["blocks"] if block["type"] == "actions"]
    assert [button["action_id"] for button in buttons] == ["cancel_leave", "cancel_leave"]
    assert buttons[0]["text"]["text"].startswith("Cancel request")
    assert buttons[1]["text"]["text"].startswith("Request cancellation")


def test_approved_cancellation_waits_for_manager_and_restores_balance(db: Session) -> None:
    employee, manager, _hr = seed_people(db)
    request = LeaveRequest(
        employee_id=employee.id,
        leave_type="annual",
        start_date=date(2026, 7, 6),
        end_date=date(2026, 7, 7),
        days_requested=2,
        status=LeaveRequestStatus.approved.value,
    )
    db.add(request)
    db.flush()
    service = LeaveRequestService(db)

    assert service.request_cancellation(employee, request)
    db.flush()
    assert request.status == LeaveRequestStatus.pending_cancellation_manager.value
    assert BalanceService(db).get_taken_days(employee.id, "annual", 2026) == 2

    service.record_cancellation_decision(manager, request, approved=True)
    db.flush()
    assert request.status == LeaveRequestStatus.cancelled.value
    assert BalanceService(db).get_taken_days(employee.id, "annual", 2026) == 0
    assert db.query(ApprovalEvent).filter_by(leave_request_id=request.id).count() == 2


def test_pending_request_cancels_immediately(db: Session) -> None:
    employee, _manager, _hr = seed_people(db)
    request = LeaveRequest(
        employee_id=employee.id,
        leave_type="annual",
        start_date=date(2026, 7, 6),
        end_date=date(2026, 7, 7),
        days_requested=2,
        status=LeaveRequestStatus.pending_manager.value,
    )
    db.add(request)
    db.flush()

    assert not LeaveRequestService(db).request_cancellation(employee, request)
    assert request.status == LeaveRequestStatus.cancelled.value


def test_hr_balance_adjustment_changes_allocation_and_is_audited(db: Session) -> None:
    employee, _manager, hr = seed_people(db)
    employee.workspace_id = hr.workspace_id = "T_TEST"
    service = BalanceService(db)

    adjustment = service.adjust_allocation(
        hr,
        employee,
        "annual",
        2026,
        5,
        "Contract entitlement correction",
    )

    assert isinstance(adjustment, LeaveBalanceAdjustment)
    assert adjustment.adjusted_by_id == hr.id
    assert service.get_allocated_days(employee.id, "annual", 2026) == 25
    assert service.get_remaining_days(employee.id, "annual", 2026) == 25


def test_balance_adjustment_changes_request_entitlement(db: Session) -> None:
    employee, _manager, hr = seed_people(db)
    employee.workspace_id = hr.workspace_id = "T_TEST"
    BalanceService(db).adjust_allocation(
        hr,
        employee,
        "annual",
        2026,
        5,
        "Additional contractual allowance",
    )

    request = LeaveRequestService(db).create_request(
        LeaveRequestCreate(
            employee_id=employee.id,
            leave_type="annual",
            start_date=date(2026, 8, 3),
            end_date=date(2026, 8, 31),
            reason="Extended leave",
        )
    )

    assert float(request.days_requested) == 21


def test_hr_message_exposes_balance_and_override_controls(db: Session, monkeypatch) -> None:
    employee, _manager, hr = seed_people(db)
    employee.workspace_id = hr.workspace_id = "T_TEST"
    monkeypatch.setattr(routes, "_sync_policy_from_db", lambda session: None)

    result = routes._process_chat(
        routes.ChatIn(
            slack_user_id=hr.slack_user_id,
            workspace_id=hr.workspace_id,
            text="I need to adjust an employee balance",
        ),
        db,
    )

    assert result["type"] == "balance_report_menu"
    assert result["can_manage"] is True
    assert "Adjust balance" in result["reply"]
    assert "Override request" in result["reply"]


def test_hr_adjustment_and_override_modals_run_through_jobs(
    db: Session,
    monkeypatch,
) -> None:
    employee, _manager, hr = seed_people(db)
    employee.workspace_id = hr.workspace_id = "T_TEST"
    request = LeaveRequest(
        employee_id=employee.id,
        leave_type="annual",
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 8),
        days_requested=2,
        status=LeaveRequestStatus.rejected.value,
    )
    db.add(request)
    db.flush()
    monkeypatch.setattr(routes, "_sync_policy_from_db", lambda session: None)

    adjustment_result = routes._submit_balance_adjustment(
        {
            "user": {"id": hr.slack_user_id},
            "team": {"id": "T_TEST"},
            "view": {
                "id": "V_ADJUST",
                "state": {
                    "values": {
                        "employee": {
                            "balance_employee_search": {
                                "selected_option": {"value": str(employee.id)}
                            }
                        },
                        "leave_type": {
                            "adjustment_leave_type": {
                                "selected_option": {"value": "annual"}
                            }
                        },
                        "days": {"adjustment_days": {"value": "2"}},
                        "reason": {
                            "adjustment_reason": {"value": "Contract correction"}
                        },
                    }
                },
            },
        },
        db,
    )
    assert adjustment_result == {"response_action": "clear"}
    adjustment_job = db.scalar(
        select(DurableJob).where(DurableJob.job_type == "adjust_leave_balance")
    )
    job_handlers._adjust_leave_balance(
        db,
        adjustment_job,
        json.loads(adjustment_job.payload_json),
    )
    assert BalanceService(db).get_allocated_days(employee.id, "annual", 2026) == 22

    override_result = routes._submit_request_override(
        {
            "user": {"id": hr.slack_user_id},
            "team": {"id": "T_TEST"},
            "view": {
                "id": "V_OVERRIDE",
                "state": {
                    "values": {
                        "request": {
                            "override_request_search": {
                                "selected_option": {"value": str(request.id)}
                            }
                        },
                        "status": {
                            "override_status": {
                                "selected_option": {"value": "approved"}
                            }
                        },
                        "reason": {
                            "override_reason": {"value": "Reviewed by HR"}
                        },
                    }
                },
            },
        },
        db,
    )
    assert override_result == {"response_action": "clear"}
    override_job = db.scalar(
        select(DurableJob).where(DurableJob.job_type == "override_leave_request")
    )
    job_handlers._override_leave_request(
        db,
        override_job,
        json.loads(override_job.payload_json),
    )
    assert request.status == LeaveRequestStatus.approved.value
    assert db.scalar(
        select(func.count())
        .select_from(ApprovalEvent)
        .where(
            ApprovalEvent.leave_request_id == request.id,
            ApprovalEvent.decision == "override_approved",
        )
    ) == 1


def test_hr_override_is_workspace_scoped_and_audited(db: Session) -> None:
    employee, _manager, hr = seed_people(db)
    employee.workspace_id = hr.workspace_id = "T_TEST"
    request = LeaveRequest(
        employee_id=employee.id,
        leave_type="annual",
        start_date=date(2026, 7, 6),
        end_date=date(2026, 7, 7),
        days_requested=2,
        status=LeaveRequestStatus.rejected.value,
    )
    db.add(request)
    db.flush()

    LeaveRequestService(db).override_request(
        hr,
        request,
        "approved",
        "Approved after policy review",
    )
    db.flush()

    event_row = db.query(ApprovalEvent).filter_by(leave_request_id=request.id).one()
    assert request.status == LeaveRequestStatus.approved.value
    assert event_row.decision == "override_approved"
    assert event_row.comment == "Approved after policy review"

    hr.workspace_id = "T_OTHER"
    with pytest.raises(ValueError, match="not allowed"):
        LeaveRequestService(db).override_request(
            hr,
            request,
            "cancelled",
            "Wrong workspace",
        )
