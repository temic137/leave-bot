from datetime import date, timedelta
import json

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters.performance import PerformanceAPIClient, PerformanceAPIError
from app.core.config import settings
from app.db.models import (
    DurableJob,
    Employee,
    LeaveBalanceAdjustment,
    LeaveRequest,
)
from app.db.session import Base
from app.schemas.leave import LeaveRequestCreate
from app.services import job_handlers
from app.services.balances import BalanceService
from app.services.employee_sync import EmployeeSyncService
from app.services.leave_requests import LeaveRequestService
from app.services.policy import LeavePolicy


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


class FakePerformanceAPI:
    def __init__(self):
        self.balance = {
            "id": 7,
            "names": "Employee",
            "email": "employee@example.com",
            "leavetype": "NG Annual Leave",
            "country": "Nigeria",
            "balance": 14,
        }
        self.requests = [
            {
                "id": 8,
                "email": "employee@example.com",
                "leavetype": "NG Annual Leave",
                "startdate": "2026-08-03",
                "enddate": "2026-08-04",
                "status": "Approved",
            }
        ]
        self.created = []
        self.updated = []

    def list_balances(self, email=None):
        return (
            [self.balance]
            if email is None or email.lower() == self.balance["email"]
            else []
        )

    def list_requests(self, email=None):
        return [
            row
            for row in self.requests
            if email is None or row["email"].lower() == email.lower()
        ]

    def find_balance(self, email, local_leave_type, display_name):
        return (
            self.balance
            if PerformanceAPIClient.matches_leave_type(
                self.balance["leavetype"],
                local_leave_type,
                display_name,
            )
            and email.lower() == self.balance["email"]
            else None
        )

    def find_request(self, **kwargs):
        return None

    def create_leave_request(self, **kwargs):
        self.created.append(kwargs)
        return {"id": 44, **kwargs}

    def update_leave_request(self, request_id, **kwargs):
        self.updated.append((request_id, kwargs))
        return {"id": request_id, **kwargs}

    def update_balance(self, balance_id, value):
        self.balance["balance"] = value
        return self.balance


def seed_people(db: Session) -> tuple[Employee, Employee, Employee]:
    manager = Employee(
        workspace_id="T_TEST",
        slack_user_id="U_MANAGER",
        email="manager@example.com",
        name="Manager",
        role="manager",
    )
    employee = Employee(
        workspace_id="T_TEST",
        slack_user_id="U_EMPLOYEE",
        email="employee@example.com",
        name="Employee",
        manager=manager,
    )
    hr = Employee(
        workspace_id="T_TEST",
        slack_user_id="U_HR",
        email="hr@example.com",
        name="HR",
        role="hr",
    )
    db.add_all([manager, employee, hr])
    db.flush()
    return employee, manager, hr


def test_external_leave_type_matching() -> None:
    assert PerformanceAPIClient.matches_leave_type(
        "NG Annual Leave",
        "annual",
        "Annual Leave",
    )
    assert not PerformanceAPIClient.matches_leave_type(
        "NG Sick Leave",
        "annual",
        "Annual Leave",
    )


def test_performance_client_paginates(monkeypatch) -> None:
    calls = []

    def request(method, url, **kwargs):
        calls.append(kwargs["params"]["page"])
        page = kwargs["params"]["page"]
        return httpx.Response(
            200,
            request=httpx.Request(method, url),
            json={
                "data": [{"id": page}],
                "meta": {"totalPage": 2},
            },
        )

    monkeypatch.setattr("app.adapters.performance.httpx.request", request)

    rows = PerformanceAPIClient("https://example.test", "token").list_balances()

    assert rows == [{"id": 1}, {"id": 2}]
    assert calls == [1, 2]


def test_performance_client_reports_api_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.adapters.performance.httpx.request",
        lambda method, url, **kwargs: httpx.Response(
            200,
            request=httpx.Request(method, url),
            json={"errors": [{"message": "invalid filter"}]},
        ),
    )

    with pytest.raises(PerformanceAPIError, match="invalid filter"):
        PerformanceAPIClient("https://example.test", "token").list_requests()


def test_external_employee_sync_preserves_local_manager(db: Session) -> None:
    employee, manager, _hr = seed_people(db)

    synced = EmployeeSyncService(db).upsert_external_employee(
        {
            "id": 10284,
            "names": "Employee Updated",
            "workemail": "employee@example.com",
            "location": "NG",
            "department": "Technology",
            "role": "Employee",
            "employeestatus": "Active",
        }
    )

    assert synced.id == employee.id
    assert synced.external_employee_id == "10284"
    assert synced.country == "NG"
    assert synced.manager_id == manager.id


def test_live_balance_uses_external_remaining_balance_and_requests(
    db: Session,
    monkeypatch,
) -> None:
    employee, _manager, _hr = seed_people(db)
    fake = FakePerformanceAPI()
    monkeypatch.setattr(settings, "performance_api_mode", "live")
    service = BalanceService(db, external=fake)

    assert service.get_allocated_days(employee.id, "annual", 2026) == 16
    assert service.get_taken_days(employee.id, "annual", 2026) == 2
    assert service.get_remaining_days(employee.id, "annual", 2026) == 14
    with pytest.raises(ValueError, match="not eligible"):
        service.get_allocated_days(employee.id, "sick", 2026)


def test_live_validation_reserves_pending_local_requests(
    db: Session,
    monkeypatch,
) -> None:
    employee, _manager, _hr = seed_people(db)
    db.add(
        LeaveRequest(
            employee_id=employee.id,
            leave_type="annual",
            start_date=date(2026, 9, 7),
            end_date=date(2026, 9, 9),
            days_requested=3,
            status="pending_manager",
        )
    )
    db.flush()
    monkeypatch.setattr(settings, "performance_api_mode", "live")
    service = BalanceService(db, external=FakePerformanceAPI())

    available_for_another_request = (
        service.get_allocated_days(employee.id, "annual", 2026)
        - service.get_committed_days(employee.id, "annual", 2026)
    )

    assert available_for_another_request == 11


def test_live_balance_does_not_fall_back_to_postgres(
    db: Session,
    monkeypatch,
) -> None:
    employee, _manager, _hr = seed_people(db)

    class FailingAPI(FakePerformanceAPI):
        def find_balance(self, *args, **kwargs):
            raise PerformanceAPIError("service unavailable")

    monkeypatch.setattr(settings, "performance_api_mode", "live")
    with pytest.raises(PerformanceAPIError, match="service unavailable"):
        BalanceService(db, external=FailingAPI()).get_allocated_days(
            employee.id,
            "annual",
            2026,
        )


def test_external_request_creation_is_idempotent(
    db: Session,
    monkeypatch,
) -> None:
    employee, _manager, _hr = seed_people(db)
    request = LeaveRequest(
        employee_id=employee.id,
        leave_type="annual",
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 8),
        days_requested=2,
        status="pending_manager",
    )
    db.add(request)
    db.flush()
    fake = FakePerformanceAPI()
    fake.requests = []
    monkeypatch.setattr(job_handlers, "PerformanceAPIClient", lambda: fake)
    job = DurableJob(
        id=1,
        job_type="create_external_leave_request",
        idempotency_key="external-create",
        payload_json="{}",
    )

    job_handlers._create_external_leave_request(
        db,
        job,
        {"leave_request_id": request.id},
    )
    job_handlers._create_external_leave_request(
        db,
        job,
        {"leave_request_id": request.id},
    )

    assert request.external_request_id == "44"
    assert request.external_leave_type == "NG Annual Leave"
    assert len(fake.created) == 1
    assert db.scalar(
        select(DurableJob).where(DurableJob.job_type == "start_agentspan")
    ) is not None


def test_live_hr_adjustment_updates_external_balance_and_audit(
    db: Session,
    monkeypatch,
) -> None:
    employee, _manager, hr = seed_people(db)
    fake = FakePerformanceAPI()
    fake.requests = []
    monkeypatch.setattr(settings, "performance_api_mode", "live")
    monkeypatch.setattr(job_handlers, "PerformanceAPIClient", lambda: fake)
    monkeypatch.setattr(
        "app.services.balances.PerformanceAPIClient",
        lambda: fake,
    )
    monkeypatch.setattr(
        "app.api.routes._sync_policy_from_db",
        lambda session: None,
    )
    job = DurableJob(
        id=2,
        job_type="adjust_leave_balance",
        idempotency_key="external-adjust",
        payload_json="{}",
    )

    job_handlers._adjust_leave_balance(
        db,
        job,
        {
            "adjuster_id": hr.id,
            "employee_id": employee.id,
            "leave_type": "annual",
            "year": 2026,
            "days_delta": 2,
            "reason": "Contract correction",
            "reply_channel": hr.slack_user_id,
        },
    )

    assert fake.balance["balance"] == 16
    adjustment = db.scalar(select(LeaveBalanceAdjustment))
    assert float(adjustment.days_delta) == 2
    assert adjustment.adjusted_by_id == hr.id


def test_supplementary_policy_enforces_notice_limit_and_weekends(
    tmp_path,
    db: Session,
) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "annual": {
                    "display_name": "Annual Leave",
                    "annual_days": 20,
                    "requires_document": False,
                    "requires_hr": False,
                    "allow_negative_balance": False,
                }
            }
        ),
        encoding="utf-8",
    )
    policy = LeavePolicy(policy_path)
    policy.load_raw_text(
        "Annual Leave: 20 days maximum. No document required. "
        "Manager approval only. 3 days notice. "
        "5 days maximum per request. Weekends counted."
    )
    employee, _manager, _hr = seed_people(db)

    with pytest.raises(ValueError, match="3 days notice"):
        LeaveRequestService(db, policy).create_request(
            LeaveRequestCreate(
                employee_id=employee.id,
                leave_type="annual",
                start_date=date.today() + timedelta(days=1),
                end_date=date.today() + timedelta(days=1),
            )
        )

    start = date.today() + timedelta(days=7)
    with pytest.raises(ValueError, match="at most 5 days"):
        LeaveRequestService(db, policy).create_request(
            LeaveRequestCreate(
                employee_id=employee.id,
                leave_type="annual",
                start_date=start,
                end_date=start + timedelta(days=5),
            )
        )
