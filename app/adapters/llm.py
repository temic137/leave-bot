from datetime import date
import re

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

