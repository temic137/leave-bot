from datetime import date


def calculate_leave_days(start_date: date, end_date: date) -> float:
    if end_date < start_date:
        raise ValueError("end_date cannot be before start_date")
    return float((end_date - start_date).days + 1)

