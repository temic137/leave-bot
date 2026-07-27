from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Employee


class EmployeeSyncService:
    def __init__(self, db: Session):
        self.db = db

    def upsert_slack_user(self, slack_user_id: str, email: str, name: str, is_active: bool = True) -> Employee:
        employee = self.db.scalar(select(Employee).where(Employee.slack_user_id == slack_user_id))
        if employee is None:
            email_owner = self.db.scalar(select(Employee).where(Employee.email == email))
            stored_email = email if email_owner is None else f"{slack_user_id.lower()}@slack-id.invalid"
            employee = Employee(slack_user_id=slack_user_id, email=stored_email, name=name, is_active=is_active)
            self.db.add(employee)
        else:
            email_owner = self.db.scalar(select(Employee).where(Employee.email == email))
            if email_owner is None or email_owner.id == employee.id:
                employee.email = email
            employee.name = name
            employee.is_active = is_active
        self.db.flush()
        return employee
