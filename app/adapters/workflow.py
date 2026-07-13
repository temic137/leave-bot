from dataclasses import dataclass

import httpx

from app.core.config import settings


@dataclass(frozen=True)
class WorkflowHandle:
    execution_id: str


class AgentSpanApprovalWorkflow:
    manager_workflow = "leave_approval_manager_v1"
    hr_workflow = "leave_approval_manager_hr_v1"

    def __init__(self, server_url: str | None = None, timeout: float = 10):
        self.server_url = (server_url or settings.agentspan_server_url).rstrip("/")
        self.timeout = timeout

    @classmethod
    def configured(cls) -> bool:
        return bool(settings.agentspan_server_url)

    @classmethod
    def start_worker(cls) -> None:
        if cls.configured():
            cls().register_workflows()

    def register_workflows(self) -> None:
        self._ensure_registered(False)
        self._ensure_registered(True)

    def ensure_registered(self, requires_hr: bool) -> None:
        self._ensure_registered(requires_hr)

    def _ensure_registered(self, requires_hr: bool) -> None:
        name = self.hr_workflow if requires_hr else self.manager_workflow
        response = httpx.get(
            f"{self.server_url}/api/metadata/workflow/{name}",
            params={"version": 1},
            timeout=self.timeout,
        )
        if response.status_code == 404:
            self._request("POST", "/api/metadata/workflow", json=self._definition(requires_hr))
            return
        response.raise_for_status()

    def start(self, leave_request_id: int, requires_hr: bool) -> WorkflowHandle:
        if not self.server_url:
            raise RuntimeError("AGENTSPAN_SERVER_URL is not configured")
        workflow_name = self.hr_workflow if requires_hr else self.manager_workflow
        response = self._request(
            "POST",
            f"/api/workflow/{workflow_name}",
            params={"version": 1, "correlationId": f"leave-request-{leave_request_id}"},
            json={"leave_request_id": leave_request_id},
        )
        return WorkflowHandle(execution_id=response.text.strip('"'))

    def decide(self, execution_id: str, approved: bool, reason: str = "", stage: str | None = None) -> None:
        if not approved:
            response = httpx.request(
                "DELETE",
                self.server_url + f"/api/workflow/{execution_id}",
                params={"reason": reason or "Leave request rejected"},
                timeout=self.timeout,
            )
            if getattr(response, "status_code", 200) != 404:
                response.raise_for_status()
            return

        execution = self._request(
            "GET",
            f"/api/workflow/{execution_id}",
            params={"includeTasks": "true"},
        ).json()
        task_reference = f"{stage}_approval" if stage else None
        matching_tasks = [
            task
            for task in execution.get("tasks", [])
            if task.get("taskType") == "HUMAN"
            and (
                task_reference is None
                or (task.get("taskReferenceName") or task.get("referenceTaskName")) == task_reference
            )
        ]
        completed = [task for task in matching_tasks if task.get("status") == "COMPLETED"]
        if task_reference and completed:
            return
        active_tasks = [task for task in matching_tasks if task.get("status") == "IN_PROGRESS"]
        if len(active_tasks) != 1:
            raise RuntimeError(f"Expected one active AgentSpan {stage or ''} approval task, found {len(active_tasks)}")
        task = active_tasks[0]
        self._request(
            "POST",
            "/api/tasks",
            json={
                "workflowInstanceId": execution_id,
                "taskId": task["taskId"],
                "status": "COMPLETED",
                "outputData": {"approved": True},
            },
        )

    def status(self, execution_id: str) -> dict:
        return self._request(
            "GET",
            f"/api/workflow/{execution_id}",
            params={"includeTasks": "true"},
        ).json()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.server_url:
            raise RuntimeError("AGENTSPAN_SERVER_URL is not configured")
        response = httpx.request(method, self.server_url + path, timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response

    def _definition(self, requires_hr: bool) -> dict:
        tasks = [self._human_task("manager_approval")]
        if requires_hr:
            tasks.append(self._human_task("hr_approval"))
        return {
            "name": self.hr_workflow if requires_hr else self.manager_workflow,
            "description": "Durable leave approval managed by AgentSpan",
            "version": 1,
            "schemaVersion": 2,
            "inputParameters": ["leave_request_id"],
            "tasks": tasks,
            "outputParameters": {"leave_request_id": "${workflow.input.leave_request_id}"},
            "restartable": True,
            "ownerEmail": "leave-bot@local",
        }

    @staticmethod
    def _human_task(name: str) -> dict:
        return {
            "name": name,
            "taskReferenceName": name,
            "type": "HUMAN",
            "inputParameters": {"leave_request_id": "${workflow.input.leave_request_id}"},
        }
