from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Employee


class EmployeeSyncService:
    def __init__(self, db: Session):
        self.db = db

    def upsert_slack_user(
        self,
        slack_user_id: str,
        email: str,
        name: str,
        is_active: bool = True,
        workspace_id: str | None = None,
    ) -> Employee:
        query = select(Employee).where(Employee.slack_user_id == slack_user_id)
        if workspace_id:
            query = query.where((Employee.workspace_id == workspace_id) | (Employee.workspace_id.is_(None)))
        employee = self.db.scalar(query)
        if employee is None:
            email_owner = self.db.scalar(select(Employee).where(Employee.email == email))
            if email_owner is not None and email_owner.slack_user_id.startswith("external:"):
                employee = email_owner
                employee.slack_user_id = slack_user_id
                employee.workspace_id = workspace_id
            else:
                stored_email = email if email_owner is None else f"{slack_user_id.lower()}@slack-id.invalid"
                employee = Employee(
                    workspace_id=workspace_id,
                    slack_user_id=slack_user_id,
                    email=stored_email,
                    name=name,
                    is_active=is_active,
                )
                self.db.add(employee)
        else:
            email_owner = self.db.scalar(select(Employee).where(Employee.email == email))
            if email_owner is None or email_owner.id == employee.id:
                employee.email = email
            employee.name = name
            employee.is_active = is_active
            if workspace_id:
                employee.workspace_id = workspace_id
        self.db.flush()
        return employee

    def upsert_external_employee(self, record: dict) -> Employee:
        external_id = str(record["id"])
        email = str(record.get("workemail") or "").strip().lower()
        if not email:
            raise ValueError(f"External employee {external_id} has no work email")
        employee = self.db.scalar(
            select(Employee).where(Employee.external_employee_id == external_id)
        )
        if employee is None:
            employee = self.db.scalar(select(Employee).where(Employee.email == email))
        if employee is None:
            employee = Employee(
                external_employee_id=external_id,
                slack_user_id=f"external:{external_id}",
                email=email,
                name=str(record.get("names") or email),
            )
            self.db.add(employee)
        employee.external_employee_id = external_id
        employee.email = email
        employee.name = str(record.get("names") or employee.name)
        employee.department = record.get("department") or employee.department
        employee.country = record.get("location") or employee.country
        employee.is_active = str(record.get("employeestatus") or "Active").lower() == "active"
        external_role = str(record.get("role") or "").lower()
        if employee.role == "employee" and external_role in {"manager", "hr", "admin"}:
            employee.role = external_role
        self.db.flush()
        return employee
