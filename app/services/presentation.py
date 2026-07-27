from datetime import date

from app.services.policy import leave_policy


def leave_name(leave_type: str) -> str:
    try:
        return leave_policy.get(leave_type).display_name
    except KeyError:
        return leave_type.replace("_", " ").title()


def readable_date(value: date | str) -> str:
    parsed = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def readable_status(status: str) -> str:
    labels = {
        "pending_manager": "Waiting for manager approval",
        "pending_hr": "Waiting for HR approval",
    }
    return labels.get(status, status.replace("_", " ").title())

