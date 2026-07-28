from datetime import date


def calculate_leave_days(
    start_date: date,
    end_date: date,
    count_weekends: bool = False,
) -> float:
    if end_date < start_date:
        raise ValueError("end_date cannot be before start_date")
    if count_weekends:
        return float((end_date - start_date).days + 1)
    return float(
        sum(
            (start_date.fromordinal(day).weekday() < 5)
            for day in range(start_date.toordinal(), end_date.toordinal() + 1)
        )
    )
