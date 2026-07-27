from datetime import date

from pydantic import BaseModel


class LeaveRequestCreate(BaseModel):
    employee_id: int
    leave_type: str
    start_date: date
    end_date: date
    reason: str | None = None
    document_key: str | None = None


class LeaveRequestRead(BaseModel):
    id: int
    employee_id: int
    leave_type: str
    start_date: date
    end_date: date
    days_requested: float
    reason: str | None
    document_key: str | None
    status: str
    agentspan_execution_id: str | None

    model_config = {"from_attributes": True}


class BalanceRead(BaseModel):
    employee_id: int
    leave_type: str
    year: int
    allocated_days: float
    taken_days: float
    remaining_days: float
