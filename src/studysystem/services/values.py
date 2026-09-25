"""Checking one input value before it reaches a row. D-36: every value rule is checked here first,
and the table's CHECK is only the backstop - a CHECK failure reaches the host as one generic line.

Each checker takes the input's name and its raw value, and returns the value in its stored form,
or raises `invalid_value` naming the field. None is not their problem: a setter rejects a missing
value before calling one (D-37).
"""

import datetime
import math
import re

from studysystem.errors import StudyError


def invalid(field: str, problem: str, fix: str) -> StudyError:
    """The one error every checker raises."""
    return StudyError(
        code="invalid_value",
        message=f"{field}: {problem}",
        fix=fix,
        field_errors=[{"field": field, "problem": problem}],
    )


def number(
    field: str,
    value: object,
    *,
    gt: float | None = None,
    ge: float | None = None,
    le: float | None = None,
) -> float:
    """A float within the bounds given: `gt` is strictly above, `ge` at least, `le` at most.
    Cowork may send `"3"` for 3, so a numeric string counts."""
    # TODO(human):
    #   turn the value into a float - a number, or a string that reads as one
    #   anything else (a word, a bool, an empty string) -> invalid
    #   each bound that was given: outside it -> invalid, the problem naming the bound
    #   hand back the float
    if isinstance(value, bool):
        raise invalid(
            field, f"{value!r} is true/false, not a number", "send a number, e.g. 3 or 3.5"
        )
    if isinstance(value, str):
        value = value.strip(' ,"')

    try:
        nb = float(value)
        if not math.isfinite(nb):
            raise ValueError
    except (TypeError, ValueError):
        raise invalid(field, f"{value!r} is not a number", "send a number, e.g. 3 or 3.5") from None
    if gt is not None and nb <= gt:
        raise invalid(field, f"must be above {gt:g}, got {nb:g}", f"send a number above {gt:g}")
    if ge is not None and nb < ge:
        raise invalid(field, f"must be at least {ge:g}, got {nb:g}", f"send {ge:g} or more")
    if le is not None and nb > le:
        raise invalid(field, f"must be at most {le:g}, got {nb:g}", f"send {le:g} or less")
    return nb


def text(field: str, value: object) -> str:
    """Non-empty text, surrounding spaces stripped."""
    # TODO(human):
    #   not a string, or nothing left after stripping -> invalid
    #   hand back the stripped string

    if not isinstance(value, str):
        raise invalid(field, f"{value!r} is not text", f"send {field} as text")
    value = value.strip()
    if value == "":
        raise invalid(field, "is empty", f"send a non-empty {field}")

    return value


def day(field: str, value: object) -> str:
    """A real calendar date as `YYYY-MM-DD`."""
    # TODO(human):
    #   must be a string in exactly that shape, and a date that exists (2027-02-30 does not)
    #   hand it back unchanged
    # The regex holds the shape; fromisoformat alone also takes 20270118 and 2027-W03-1.
    # [0-9], not \d: \d also matches Arabic-Indic digits.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise invalid(field, f"{value!r} is not a YYYY-MM-DD date", "send a date like 2027-01-18")
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise invalid(field, f"{value} is not a real date", "check the day and month") from None
    return value


def day_or_month(field: str, value: object) -> tuple[str, bool]:
    """D-39: `YYYY-MM-DD` is exact; `YYYY-MM` is the 1st of that month, marked approximate.
    Returns (the date to store, whether it is approximate)."""
    # TODO(human):

    #   the exact shape -> reuse the day check, not approximate
    #   the month shape -> the 1st of that month (it must be a real month), approximate
    #   anything else -> invalid, and the fix shows both shapes
    shapes = "send YYYY-MM-DD, or YYYY-MM when only the month is known"
    if not isinstance(value, str):
        raise invalid(field, f"{value!r} is not a date", shapes)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return day(field, value), False
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        month = int(value[5:])
        if month < 1 or month > 12:
            raise invalid(field, f"{value}: there is no month {month:02d}", shapes)
        return f"{value}-01", True
    raise invalid(field, f"{value!r} is not a date", shapes)


def clock(field: str, value: object) -> str:
    """A wall-clock time, `HH:MM`, 24-hour."""
    # TODO(human):
    #   two digits, a colon, two digits; hour 00-23, minute 00-59; else invalid
    shape = "send a 24-hour time like 09:00 or 14:30"
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        raise invalid(field, f"{value!r} is not an HH:MM time", shape)
    hour, minute = int(value[:2]), int(value[3:])
    if hour > 23 or minute > 59:
        raise invalid(field, f"{value} is not a real time", shape)
    return value
