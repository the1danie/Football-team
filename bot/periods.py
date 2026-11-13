"""Периоды для рейтинга и посещаемости: календарный месяц (с 1-го числа заново), прошлый месяц, всё время."""

from datetime import datetime, timedelta

MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
PERIODS = ("month", "prev", "all")


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def prev_month_start(now: datetime) -> datetime:
    start = month_start(now)
    return start.replace(year=start.year - 1, month=12) if start.month == 1 else start.replace(month=start.month - 1)


def normalize(period: str | None) -> str:
    return period if period in PERIODS else "all"


def bounds(period: str | None, now: datetime) -> tuple[datetime, datetime | None]:
    """(конец, начало) в местном времени. Всё время — начала нет."""
    period = normalize(period)
    if period == "month":
        return now, month_start(now)
    if period == "prev":
        return month_start(now) - timedelta(microseconds=1), prev_month_start(now)
    return now, None


def label(period: str | None, now: datetime) -> str:
    """«Ноябрь», «Октябрь», «Всё время»."""
    period = normalize(period)
    if period == "month":
        return MONTHS_NOM[now.month - 1].capitalize()
    if period == "prev":
        return MONTHS_NOM[prev_month_start(now).month - 1].capitalize()
    return "Всё время"


def labels(now: datetime) -> dict[str, str]:
    return {p: label(p, now) for p in PERIODS}


def title(period: str | None, now: datetime) -> str:
    """Для PDF: «за ноябрь 2026», «за всё время»."""
    period = normalize(period)
    if period == "all":
        return "за всё время"
    start = bounds(period, now)[1]
    return f"за {MONTHS_NOM[start.month - 1]} {start.year}"
