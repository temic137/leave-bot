from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class WorkflowHandle:
    execution_id: str


class ApprovalWorkflow:
    def start(self, leave_request_id: int, requires_hr: bool) -> WorkflowHandle:
        raise NotImplementedError


class LocalApprovalWorkflow(ApprovalWorkflow):
    def start(self, leave_request_id: int, requires_hr: bool) -> WorkflowHandle:
        prefix = "local-hr" if requires_hr else "local-manager"
        return WorkflowHandle(execution_id=f"{prefix}-{leave_request_id}-{uuid4()}")

