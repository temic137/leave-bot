import time

from app.adapters.workflow import AgentSpanApprovalWorkflow


def wait_for_stage(
    workflow: AgentSpanApprovalWorkflow,
    execution_id: str,
    expected_stage: str | None = None,
    expected_status: str | None = None,
) -> dict:
    for _ in range(40):
        state = workflow.status(execution_id)
        if expected_status and state.get("status") == expected_status:
            return state
        if expected_stage and active_stage(state) == expected_stage:
            return state
        time.sleep(0.25)
    raise RuntimeError(
        f"AgentSpan execution did not reach stage={expected_stage} status={expected_status}; "
        f"last state={state.get('status')} active_stage={active_stage(state)}"
    )


def active_stage(state: dict) -> str | None:
    return next(
        (
            task["referenceTaskName"]
            for task in state.get("tasks", [])
            if task.get("taskType") == "HUMAN" and task.get("status") == "IN_PROGRESS"
        ),
        None,
    )


workflow = AgentSpanApprovalWorkflow()
workflow.register_workflows()

manager = workflow.start(920001, False)
wait_for_stage(workflow, manager.execution_id, expected_stage="manager_approval")
workflow.decide(manager.execution_id, True)
wait_for_stage(workflow, manager.execution_id, expected_status="COMPLETED")

hr = workflow.start(920002, True)
wait_for_stage(workflow, hr.execution_id, expected_stage="manager_approval")
workflow.decide(hr.execution_id, True)
wait_for_stage(workflow, hr.execution_id, expected_stage="hr_approval")
workflow.decide(hr.execution_id, True)
wait_for_stage(workflow, hr.execution_id, expected_status="COMPLETED")

rejected = workflow.start(920003, False)
wait_for_stage(workflow, rejected.execution_id, expected_stage="manager_approval")
workflow.decide(rejected.execution_id, False, "production rejection test")
wait_for_stage(workflow, rejected.execution_id, expected_status="TERMINATED")

print("AgentSpan production verification passed")
