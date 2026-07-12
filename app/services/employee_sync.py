from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Employee


class EmployeeSyncService:
    def __init__(self, db: Session):
        self.db = db

    def upsert_slack_user(self, slack_user_id: str, email: str, name: str, is_active: bool = True) -> Employee:
        employee = self.db.scalar(select(Employee).where(Employee.email == email))
        if employee is None:
            employee = Employee(slack_user_id=slack_user_id, email=email, name=name, is_active=is_active)
            self.db.add(employee)
        else:
            employee.slack_user_id = slack_user_id
            employee.name = name
            employee.is_active = is_active
        self.db.flush()
        return employee

