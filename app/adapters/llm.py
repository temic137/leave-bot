from datetime import date
import json
import re

import httpx

from app.core.config import settings
from app.schemas.leave import ParsedMessage


class MessageParser:
    def parse(self, message: str, today: date | None = None) -> ParsedMessage:
        raise NotImplementedError


class MockMessageParser(MessageParser):
    def parse(self, message: str, today: date | None = None) -> ParsedMessage:
        normalized = message.lower()
        leave_type = None
        for candidate in ("annual", "sick", "maternity", "emergency"):
            if candidate in normalized:
                leave_type = candidate
                break

        iso_dates = re.findall(r"\d{4}-\d{2}-\d{2}", message)
        start_date = date.fromisoformat(iso_dates[0]) if len(iso_dates) >= 1 else None
        end_date = date.fromisoformat(iso_dates[1]) if len(iso_dates) >= 2 else start_date

        missing = []
        if leave_type is None:
            missing.append("leave_type")
        if start_date is None:
            missing.append("start_date")
        if end_date is None:
            missing.append("end_date")

        return ParsedMessage(
            intent="create_leave_request" if "leave" in normalized else "unknown",
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            reason=message,
            confidence=0.6 if not missing else 0.3,
            missing_fields=missing,
        )


class GroqMessageParser(MessageParser):
    def parse(
        self,
        message: str,
        today: date | None = None,
        leave_types: list[str] | None = None,
        existing_fields: dict | None = None,
    ) -> ParsedMessage:
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is not configured")

        current_date = today or date.today()
        prompt = {
            "task": "Extract a leave request from a natural employee message. Return JSON only.",
            "today": current_date.isoformat(),
            "allowed_leave_types": leave_types or [],
            "previously_collected_fields": existing_fields or {},
            "employee_message": message,
            "output": {
                "intent": "create_leave_request or unknown",
                "leave_type": "one allowed leave type or null",
                "start_date": "YYYY-MM-DD or null",
                "end_date": "YYYY-MM-DD or null",
                "reason": "short reason or null",
            },
            "rules": [
                "Resolve conversational dates relative to today.",
                "Preserve previously collected fields unless the employee corrects them.",
                "Do not invent missing dates or a leave type.",
            ],
        }
        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json={
                "model": settings.groq_model,
                "messages": [{"role": "user", "content": json.dumps(prompt)}],
                "response_format": {"type": "json_object"},
                "reasoning_effort": "none",
                "temperature": 0,
                "max_completion_tokens": 300,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = json.loads(response.json()["choices"][0]["message"]["content"])
        start_date = _optional_date(data.get("start_date"))
        end_date = _optional_date(data.get("end_date"))
        leave_type = data.get("leave_type")
        missing = []
        if not leave_type:
            missing.append("leave_type")
        if not start_date:
            missing.append("start_date")
        if not end_date:
            missing.append("end_date")
        return ParsedMessage(
            intent=data.get("intent", "unknown"),
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            reason=data.get("reason") or message,
            confidence=0.85 if not missing else 0.5,
            missing_fields=missing,
        )


def _optional_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
