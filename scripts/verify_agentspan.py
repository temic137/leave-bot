import time

from app.adapters.workflow import AgentSpanApprovalWorkflow


def wait_for_stage(workflow: AgentSpanApprovalWorkflow, execution_id: str) -> dict:
    for _ in range(20):
        state = workflow.status(execution_id)
        active = [
            task
            for task in state.get("tasks", [])
            if task.get("taskType") == "HUMAN" and task.get("status") == "IN_PROGRESS"
        ]
        if active or state["status"] != "RUNNING":
            return state
        time.sleep(0.25)
    raise RuntimeError("AgentSpan execution did not reach a stable stage")


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
assert active_stage(wait_for_stage(workflow, manager.execution_id)) == "manager_approval"
workflow.decide(manager.execution_id, True)
assert wait_for_stage(workflow, manager.execution_id)["status"] == "COMPLETED"

hr = workflow.start(920002, True)
assert active_stage(wait_for_stage(workflow, hr.execution_id)) == "manager_approval"
workflow.decide(hr.execution_id, True)
assert active_stage(wait_for_stage(workflow, hr.execution_id)) == "hr_approval"
workflow.decide(hr.execution_id, True)
assert wait_for_stage(workflow, hr.execution_id)["status"] == "COMPLETED"

rejected = workflow.start(920003, False)
assert active_stage(wait_for_stage(workflow, rejected.execution_id)) == "manager_approval"
workflow.decide(rejected.execution_id, False, "production rejection test")
assert wait_for_stage(workflow, rejected.execution_id)["status"] == "TERMINATED"

print("AgentSpan production verification passed")
