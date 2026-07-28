from datetime import date
from typing import Any

import httpx

from app.core.config import settings


class PerformanceAPIError(RuntimeError):
    pass


class PerformanceAPIClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
    ):
        self.base_url = (base_url or settings.performance_api_url).rstrip("/")
        self.token = token or settings.performance_api_token
        self.timeout = timeout or settings.performance_api_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def list_employees(self) -> list[dict]:
        return self._list("employee")

    def list_balances(self, email: str | None = None) -> list[dict]:
        params = {"filter[email]": email} if email else None
        return self._list("leavebalance", params)

    def list_requests(self, email: str | None = None) -> list[dict]:
        params = {"filter[email]": email} if email else None
        return self._list("leaverequests", params)

    def find_balance(
        self,
        email: str,
        local_leave_type: str,
        display_name: str,
    ) -> dict | None:
        return next(
            (
                row
                for row in self.list_balances(email)
                if self.matches_leave_type(
                    str(row.get("leavetype") or ""),
                    local_leave_type,
                    display_name,
                )
            ),
            None,
        )

    def find_request(
        self,
        *,
        email: str,
        leave_type: str,
        start_date: date,
        end_date: date,
    ) -> dict | None:
        return next(
            (
                row
                for row in self.list_requests(email)
                if str(row.get("leavetype") or "").lower() == leave_type.lower()
                and str(row.get("startdate") or "")[:10] == start_date.isoformat()
                and str(row.get("enddate") or "")[:10] == end_date.isoformat()
            ),
            None,
        )

    def create_leave_request(
        self,
        *,
        email: str,
        employee_name: str,
        leave_type: str,
        start_date: date,
        end_date: date,
        status: str,
        country: str,
        approver_name: str,
        approver_email: str,
    ) -> dict:
        return self._request(
            "POST",
            "/api/leaverequests:create",
            json={
                "email": email,
                "Names": employee_name,
                "leavetype": leave_type,
                "startdate": start_date.isoformat(),
                "enddate": end_date.isoformat(),
                "status": status,
                "country": country,
                "approver": approver_name,
                "approveremail": approver_email,
            },
        ).get("data") or {}

    def update_leave_request(
        self,
        request_id: str,
        *,
        status: str,
        approver_name: str,
        approver_email: str,
    ) -> dict:
        return self._request(
            "POST",
            "/api/leaverequests:update",
            params={"filterByTk": request_id},
            json={
                "status": status,
                "approver": approver_name,
                "approveremail": approver_email,
            },
        ).get("data") or {}

    def update_balance(self, balance_id: str, value: float) -> dict:
        return self._request(
            "POST",
            "/api/leavebalance:update",
            params={"filterByTk": balance_id},
            json={"balance": value},
        ).get("data") or {}

    @staticmethod
    def matches_leave_type(
        external_name: str,
        local_key: str,
        display_name: str,
    ) -> bool:
        external = " ".join(external_name.lower().split())
        key = " ".join(local_key.replace("_", " ").lower().split())
        display = " ".join(display_name.lower().split())
        return external in {key, display} or external.endswith(f" {display}")

    def _list(self, resource: str, params: dict[str, Any] | None = None) -> list[dict]:
        page = 1
        rows: list[dict] = []
        while True:
            page_params = {**(params or {}), "page": page, "pageSize": 200}
            payload = self._request(
                "GET",
                f"/api/{resource}:list",
                params=page_params,
            )
            rows.extend(payload.get("data") or [])
            total_pages = int((payload.get("meta") or {}).get("totalPage") or 0)
            if page >= total_pages:
                return rows
            page += 1

    def _request(self, method: str, path: str, **kwargs) -> dict:
        if not self.configured:
            raise PerformanceAPIError("Performance API is not configured")
        try:
            response = httpx.request(
                method,
                self.base_url + path,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PerformanceAPIError(f"Performance API request failed: {exc}") from exc
        if payload.get("errors"):
            message = "; ".join(
                str(error.get("message") or error)
                for error in payload["errors"]
            )
            raise PerformanceAPIError(f"Performance API rejected the request: {message}")
        return payload
