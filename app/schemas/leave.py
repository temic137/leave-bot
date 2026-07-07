from datetime import date

from pydantic import BaseModel, Field


class ParsedMessage(BaseModel):
    intent: str = "unknown"
    leave_type: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    reason: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    missing_fields: list[str] = Field(default_factory=list)


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
    taken_days: float
