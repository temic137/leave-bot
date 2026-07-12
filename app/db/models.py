from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class EmployeeRole(StrEnum):
    employee = "employee"
    manager = "manager"
    hr = "hr"
    admin = "admin"


class LeaveRequestStatus(StrEnum):
    draft = "draft"
    pending_manager = "pending_manager"
    pending_hr = "pending_hr"
    approved = "approved"
    rejected = "rejected"
    cancelled = "cancelled"


class ApprovalDecision(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default=EmployeeRole.employee.value)
    department: Mapped[str | None] = mapped_column(String(128))
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    manager: Mapped["Employee | None"] = relationship(remote_side=[id])


class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    leave_type: Mapped[str] = mapped_column(String(64), index=True)
    start_date: Mapped[datetime] = mapped_column(Date)
    end_date: Mapped[datetime] = mapped_column(Date)
    days_requested: Mapped[float] = mapped_column(Numeric(6, 2))
    reason: Mapped[str | None] = mapped_column(Text)
    document_key: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default=LeaveRequestStatus.pending_manager.value)
    agentspan_execution_id: Mapped[str | None] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)

    employee: Mapped[Employee] = relationship()


class LeaveBalanceLedger(Base):
    __tablename__ = "leave_balance_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    leave_type: Mapped[str] = mapped_column(String(64), index=True)
    year: Mapped[int] = mapped_column(index=True)
    change_days: Mapped[float] = mapped_column(Numeric(6, 2))
    reason: Mapped[str] = mapped_column(String(255))
    leave_request_id: Mapped[int | None] = mapped_column(ForeignKey("leave_requests.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ApprovalEvent(Base):
    __tablename__ = "approval_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    leave_request_id: Mapped[int] = mapped_column(ForeignKey("leave_requests.id"), index=True)
    approver_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    approver_role: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(32), default=ApprovalDecision.pending.value)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ConversationSession(Base):
    __tablename__ = "conversation_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(String(64), index=True)
    current_intent: Mapped[str | None] = mapped_column(String(64))
    collected_fields_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="open")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class LeavePolicyVersion(Base):
    __tablename__ = "leave_policy_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(unique=True, index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    rules_json: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(255), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
