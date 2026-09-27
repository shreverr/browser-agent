"""Relative-date placeholders in task text and scripted replies, resolved at Trial time.

{today}                  today
{today+10d} {today+14m}  offset by days or calendar months
{next_saturday}          the next Saturday strictly after today
{second_friday_next_month} / {last_sunday_next_month}   nth weekday of next month
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from typing import Any

WEEKDAYS = [name.lower() for name in calendar.day_name]
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}
_PLACEHOLDER = re.compile(r"\{([a-z_]+)([+-]\d+[dm])?\}")


def fmt(day: dt.date) -> str:
    return f"{day:%A} {day.day} {day:%B %Y}"


def _add_months(day: dt.date, months: int) -> dt.date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> dt.date:
    days = [
        dt.date(year, month, d)
        for d in range(1, calendar.monthrange(year, month)[1] + 1)
        if dt.date(year, month, d).weekday() == weekday
    ]
    return days[nth - 1] if nth > 0 else days[nth]


def resolve_date(name: str, offset: str | None, today: dt.date) -> dt.date:
    if name == "today":
        if not offset:
            return today
        amount, unit = int(offset[:-1]), offset[-1]
        return today + dt.timedelta(days=amount) if unit == "d" else _add_months(today, amount)
    if name.startswith("next_") and name[5:] in WEEKDAYS:
        ahead = (WEEKDAYS.index(name[5:]) - today.weekday() - 1) % 7 + 1
        return today + dt.timedelta(days=ahead)
    match = re.fullmatch(r"([a-z]+)_([a-z]+)_next_month", name)
    if match and match[1] in ORDINALS and match[2] in WEEKDAYS:
        first = _add_months(today.replace(day=1), 1)
        return _nth_weekday(first.year, first.month, WEEKDAYS.index(match[2]), ORDINALS[match[1]])
    raise ValueError(f"unknown date placeholder {{{name}{offset or ''}}}")


def resolve_text(text: str, today: dt.date) -> str:
    return _PLACEHOLDER.sub(lambda m: fmt(resolve_date(m[1], m[2], today)), text)


def resolve_task(task: dict[str, Any], today: dt.date) -> dict[str, Any]:
    """Return a copy with dates resolved; the template stays in `text_template`."""
    resolved = dict(task)
    resolved["text_template"] = task["text"]
    resolved["text"] = resolve_text(task["text"], today)
    resolved["replies"] = [
        {**rule, "reply": resolve_text(rule["reply"], today)} for rule in task.get("replies") or []
    ]
    resolved["resolved_on"] = today.isoformat()
    return resolved
