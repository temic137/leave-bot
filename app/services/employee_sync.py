import csv
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Employee, ManagerMapping


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

    def import_manager_mapping_csv(self, path: str | Path) -> int:
        count = 0
        with Path(path).open("r", encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            for row in reader:
                mapping = self.db.scalar(select(ManagerMapping).where(ManagerMapping.employee_email == row["employee_email"]))
                if mapping is None:
                    mapping = ManagerMapping(employee_email=row["employee_email"])
                    self.db.add(mapping)
                mapping.manager_email = row.get("manager_email") or None
                mapping.role = row.get("role") or "employee"
                mapping.department = row.get("department") or None
                count += 1
        self.db.flush()
        return count

    def apply_manager_mappings(self) -> int:
        updated = 0
        mappings = self.db.scalars(select(ManagerMapping)).all()
        for mapping in mappings:
            employee = self.db.scalar(select(Employee).where(Employee.email == mapping.employee_email))
            if employee is None:
                continue
            manager = None
            if mapping.manager_email:
                manager = self.db.scalar(select(Employee).where(Employee.email == mapping.manager_email))
            employee.manager_id = manager.id if manager else None
            employee.role = mapping.role
            employee.department = mapping.department
            updated += 1
        self.db.flush()
        return updated

