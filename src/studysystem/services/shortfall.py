"""The shortfall detector and the three levers (4.9; D-15, D-292 - D-300).

Hours-to-all-targets against declared capacity, read on demand - nothing is stored (no offer
table). SQL gathers the rows (`_gather`); pure functions do the arithmetic and build the alert,
so the math is tested without a database, as in plan.py. Only `respond_to_offer` writes.

Who reads it: `GET /state` (its `shortfall` section) and the `study_get_plan` reply, so the
dashboard and the host both see the alert. He answers it through a tool - nothing here changes
his capacity, targets or strategy (D-15, D-299).
"""

import datetime
from collections.abc import Sequence

from sqlalchemy import Connection, Engine, and_, case, func, select, update

from studysystem.db.tables import assessment, assessment_slot, capacity, course, hours_entry
from studysystem.errors import StudyError
from studysystem.services.ids import now
from studysystem.services.lookups import find_course
from studysystem.services.units import write_unit
from studysystem.services.values import invalid

H_DEFAULT = 6.0  # hours per credit for a 75% target, inferred: ~2 h a week per 4-credit (D-294)
PASS_MARK = 50.0  # a conceded course's need is read at the pass mark (D-299)
PACE_DAYS = 14  # the pace is read over the last 14 days (D-298)
LOWER_BY = 10  # the target lever's hours are shown for a target this many points lower (D-300)


# --- the gather (SQL) --------------------------------------------------------------------------


def _gather(conn: Connection, user_id: str, today: datetime.date) -> dict:
    """Everything the detector reads, as plain values, from one moment. Writes nothing.

    Returns {
      "courses": [one dict per course in play - planned and not dropped (D-216, D-217) - with
                  course_id, code, credits, target_grade, conceded (bool), strategy,
                  hours_per_credit, hours_per_credit_tier, elective (bool),
                  minutes (all logged, not voided), recent_minutes (logged in the last
                  PACE_DAYS days, not voided)],
      "capacity": [(from_date, hours_per_week), ...] every capacity row, oldest first, ties by
                  id - the later id wins a tie (the capacity rule),
      "last_exam": the latest upcoming exam date (a date) among the courses in play, or None,
    }

    "The last 14 days" compares `occurred_at` against the UTC midnight 13 days before `today`,
    so today and the 13 days before it count.
    """
    since = (today - datetime.timedelta(days=PACE_DAYS - 1)).isoformat()
    logged = (
        select(
            hours_entry.c.course_id,
            func.sum(hours_entry.c.minutes).label("minutes"),
            func.sum(
                case((hours_entry.c.occurred_at >= since, hours_entry.c.minutes), else_=0)
            ).label("recent_minutes"),
        )
        .where(hours_entry.c.user_id == user_id, hours_entry.c.voided_at.is_(None))
        .group_by(hours_entry.c.course_id)
        .subquery()
    )
    rows = conn.execute(
        select(
            course.c.id.label("course_id"),
            course.c.code,
            course.c.credits,
            course.c.target_grade,
            course.c.conceded,
            course.c.strategy,
            course.c.hours_per_credit,
            course.c.hours_per_credit_tier,
            course.c.elective,
            func.coalesce(logged.c.minutes, 0).label("minutes"),
            func.coalesce(logged.c.recent_minutes, 0).label("recent_minutes"),
        )
        .join_from(course, logged, logged.c.course_id == course.c.id, isouter=True)
        .where(
            course.c.user_id == user_id,
            course.c.planned == 1,
            course.c.dropped_at.is_(None),
        )
        .order_by(course.c.code)
    ).mappings()
    courses = [
        dict(row, conceded=bool(row["conceded"]), elective=bool(row["elective"])) for row in rows
    ]

    capacity_rows = conn.execute(
        select(capacity.c.from_date, capacity.c.hours_per_week)
        .where(capacity.c.user_id == user_id)
        .order_by(capacity.c.from_date, capacity.c.id)
    ).all()

    last = conn.execute(
        select(func.max(assessment.c.date))
        .join_from(assessment, assessment_slot, assessment_slot.c.id == assessment.c.slot_id)
        .join(course, course.c.id == assessment_slot.c.course_id)
        .where(
            and_(
                assessment.c.user_id == user_id,
                assessment_slot.c.kind == "exam",
                assessment.c.date >= today.isoformat(),
                course.c.planned == 1,
                course.c.dropped_at.is_(None),
            )
        )
    ).scalar()

    return {
        "courses": courses,
        "capacity": [(row.from_date, row.hours_per_week) for row in capacity_rows],
        "last_exam": datetime.date.fromisoformat(last) if last else None,
    }


# --- the core (pure) --------------------------------------------------------------------------


def course_need(
    credits: float | None,
    hours_per_credit: float,
    target_grade: float | None,
    conceded: bool,
    minutes_logged: int,
) -> float | None:
    """One course's hours still needed to reach its target, from today (D-292). Pure.

    need = credits x hours_per_credit x target / 100 - minutes_logged / 60, never below 0.
    A conceded course reads target PASS_MARK, whatever its declared target (D-299).
    None when credits or target_grade is unknown: the course is left out and named, never
    counted at a guess (unknown stays unknown).

    Examples, his data on 2026-11-15:
    - I3302: 4 credits, H 6, target 75, 70 min logged      -> 18 - 1.17 = 16.83
    - I3351: 5 credits, H 6, target 75, 0 logged            -> 22.5
    - I3351 with no target grade                            -> None
    - I3304 conceded: 4, H 6, target 75 (read at 50), 235   -> 12 - 3.92 = 8.08
    - DHR300 at its own H 2.7: 3, 2.7, 75, 0                -> 6.08
    - a course logged past its need: 3, 6, 75, 1000 min     -> 0.0, not -3.17
    - conceded with a declared target under the pass mark: 3, 6, 40 -> read at 40, never raised
    """
    if conceded:
        # concession lowers a target to the pass mark - it never raises one already below it
        target = PASS_MARK if target_grade is None else min(target_grade, PASS_MARK)
    else:
        target = target_grade
    if credits is None or target is None:
        return None
    return max(credits * hours_per_credit * target / 100 - minutes_logged / 60, 0.0)


def capacity_until(
    rows: Sequence[tuple[str, float]], today: datetime.date, last_exam: datetime.date
) -> float:
    """The hours his declared capacity gives from `today` up to `last_exam` (D-293). Pure.

    Each day from today (counted) up to the exam day (not counted) gets the hours_per_week / 7
    of the row in force that day: the latest from_date on or before it. Rows may come in any
    order; on a tie the later row in the list wins (`_gather` orders ties by id, the capacity
    rule). A day before every row gives 0.

    Examples - his rows: ("2026-09-27", 10), ("2026-11-02", 14.17), ("2026-12-24", 21),
    ("2027-01-04", 10):
    - today 2026-11-15, last exam 2027-01-18 -> 39 x 14.17/7 + 11 x 21/7 + 14 x 10/7 = 131.95
    - today 2026-11-15, last exam 2026-12-07 -> 22 x 14.17/7 = 44.53
    - a row ("2026-11-15", 8) added on the copy -> 39 x 8/7 + 33 + 20 = 97.57
    - today on the exam day                    -> 0.0
    """
    total = 0.0
    day = today
    while day < last_exam:
        stamp = day.isoformat()
        # the row with the latest start on or before this day; `>=` lets a later tie win
        in_force = None
        for from_date, hours in rows:
            if from_date <= stamp and (in_force is None or from_date >= in_force[0]):
                in_force = (from_date, hours)
        if in_force is not None:
            total += in_force[1] / 7
        day += datetime.timedelta(days=1)
    return total


def behind_by(need: float, days_left: int, recent_minutes: int) -> float:
    """How far a course's last PACE_DAYS days fall under the pace of its need (D-295, D-298).
    Pure. Positive = behind by that many hours; 0 or below = on pace or ahead.

    pace per day = need / days_left; behind = pace x PACE_DAYS - recent_minutes / 60.
    `days_left` is from today to the last exam, at least 1.

    Examples, 64 days to 2027-01-18:
    - I3351: need 22.5, 0 recent minutes      -> 22.5 / 64 x 14 = 4.92
    - I3304: need 14.08, 100 recent minutes   -> 3.08 - 1.67 = 1.41
    - I3303: need 15.23, 166 recent minutes   -> 3.33 - 2.77 = 0.56
    - need 0 (target reached)                 -> 0 - recent / 60, never behind
    """
    pace_per_day = need / max(days_left, 1)
    return pace_per_day * PACE_DAYS - recent_minutes / 60


def shortfall_of(gathered: dict, today: datetime.date) -> dict:
    """The alert: need against capacity, who is behind, and the ladder of offers (D-15). Pure:
    `gathered` is `_gather`'s dict.

    Returns {
      "need": total hours of the counted courses, "need_tier": "inferred",
      "capacity": capacity_until(...), "capacity_tier": "declared",
      "gap": capacity - need (below 0 = short),
      "short": gap < 0,
      "not_counted": [{"code", "reason"}] - no target grade, or no credits,
      "courses": [per counted course: code, credits, need, h (its H) + h_tier ("declared",
                  or "inferred" for the default), behind (hours, from behind_by), strategy,
                  conceded, elective, frees_conceding (hours conceding frees: 0 when already
                  conceded), frees_lowering (hours a target LOWER_BY points lower frees: 0 when
                  conceded - its need already reads at the pass mark)],
                  listed by credits descending, then code, electives last (D-300),
      "offers": the ladder, in order, each {"kind", "tool", "hours"} (D-15, D-295, D-296):
         1. {"kind": "catch-up", "tool": "study_respond_to_offer", "code", "hours": how far
            behind} for the course furthest behind (most hours) that is behind (> 0) and not
            on catch-up already; none when nobody is behind. Offered whether or not the total
            is short: one course behind is the first rung.
         2-4. only when short, "hours" = the gap as a positive number:
            {"kind": "lower-target", "tool": "study_set_course_input"},
            {"kind": "add-capacity", "tool": "study_set_capacity"},
            {"kind": "concede", "tool": "study_respond_to_offer"}.
    }
    With no upcoming exam (`last_exam` None) there is no window: need and the list still read,
    capacity, gap and short are None with tier unknown, and no offer is made.

    Examples, his data 2026-11-15, H 6 (DHR300 at its own 2.7), I3351 not yet given a target:
    - need 93.22 (I3351 not counted: no target grade), capacity 131.95, gap +38.73, short false;
      offers: catch-up for the furthest behind (I3301, 3.8 - 0.67 = 3.13), nothing more.
    - the Done-when copy: I3351 at 5 credits / 75, a capacity row ("2026-11-15", 8) -> need
      115.72, capacity 97.57, gap -18.15, short true; offers: catch-up I3351 (4.92), then the
      three levers; DHR300 (elective, H 2.7) ends the course list.
    """
    last_exam = gathered["last_exam"]
    days_left = (last_exam - today).days if last_exam is not None else None

    courses, not_counted = [], []
    for row in gathered["courses"]:
        entry = _course_entry(row, days_left)
        if entry is None:
            # every input it lacks; a conceded course reads at 50, so it never lacks a target
            missing = []
            if row["credits"] is None:
                missing.append("no credits")
            if row["target_grade"] is None and not row["conceded"]:
                missing.append("no target grade")
            not_counted.append({"code": row["code"], "reason": " and ".join(missing)})
        else:
            courses.append(entry)
    courses.sort(key=lambda c: (c["elective"], -c["credits"], c["code"]))  # electives last
    need = sum(c["need"] for c in courses)

    answer = {
        "need": need,
        "need_tier": "inferred",
        "not_counted": not_counted,
        "courses": courses,
    }
    if last_exam is None:  # no window to compare against
        return answer | {
            "capacity": None,
            "capacity_tier": "unknown",
            "gap": None,
            "short": None,
            "offers": [],
        }

    capacity_hours = capacity_until(gathered["capacity"], today, last_exam)
    gap = capacity_hours - need
    return answer | {
        "capacity": capacity_hours,
        "capacity_tier": "declared",
        "gap": gap,
        "short": gap < 0,
        "offers": _offers(courses, gap),
    }


def _course_entry(row: dict, days_left: int | None) -> dict | None:
    """One gathered course as the alert lists it, or None when it is not counted (no credits or
    no target). Pure. `days_left` None (no exam ahead) leaves `behind` None."""
    declared = row["hours_per_credit"] is not None
    h = row["hours_per_credit"] if declared else H_DEFAULT
    need = course_need(row["credits"], h, row["target_grade"], row["conceded"], row["minutes"])
    if need is None:
        return None
    if row["conceded"]:  # already at the pass mark: neither lever frees more
        frees_conceding = frees_lowering = 0
    else:
        frees_conceding = max(row["credits"] * h * (row["target_grade"] - PASS_MARK) / 100, 0)
        frees_lowering = row["credits"] * h * LOWER_BY / 100
    return {
        "course_id": row["course_id"],  # /state keys courses by id, with the code beside (D-163)
        "code": row["code"],
        "credits": row["credits"],
        "need": need,
        "h": h,
        "h_tier": "declared" if declared else "inferred",
        "behind": None if days_left is None else behind_by(need, days_left, row["recent_minutes"]),
        "strategy": row["strategy"],
        "conceded": row["conceded"],
        "elective": row["elective"],
        "frees_conceding": frees_conceding,
        "frees_lowering": frees_lowering,
    }


LEVERS = (
    ("lower-target", "study_set_course_input"),
    ("add-capacity", "study_set_capacity"),
    ("concede", "study_respond_to_offer"),
)


def _offers(courses: list[dict], gap: float) -> list[dict]:
    """The ladder (D-15): catch-up for the course furthest behind that is not on it yet, then
    - only when the total is short - the three levers, each naming the gap. Pure."""
    offers = []
    behind = [c for c in courses if c["behind"] > 0 and c["strategy"] != "catch-up"]
    if behind:
        furthest = max(behind, key=lambda c: c["behind"])  # a tie keeps the list's first
        offers.append(
            {
                "kind": "catch-up",
                "tool": "study_respond_to_offer",
                "code": furthest["code"],
                "hours": furthest["behind"],
            }
        )
    if gap < 0:
        offers += [{"kind": kind, "tool": tool, "hours": -gap} for kind, tool in LEVERS]
    return offers


def shortfall(conn: Connection, user_id: str, today: datetime.date) -> dict:
    """Read the alert for `today`: `_gather`, then `shortfall_of`, every hour figure rounded to
    2 places for the reader (the pure pieces keep full precision). Writes nothing."""
    return _rounded(shortfall_of(_gather(conn, user_id, today), today))


HOUR_FIELDS = ("need", "capacity", "gap", "h", "behind", "frees_conceding", "frees_lowering")


def _rounded(alert: dict) -> dict:
    """`alert` with each hour figure - top level, per course and per offer - to 2 places."""

    def two(record: dict, fields: Sequence[str]) -> dict:
        return {
            k: round(v, 2) if k in fields and isinstance(v, float) else v for k, v in record.items()
        }

    return two(alert, HOUR_FIELDS) | {
        "courses": [two(c, HOUR_FIELDS) for c in alert["courses"]],
        "offers": [two(o, ("hours",)) for o in alert["offers"]],
    }


# --- the answer (one write unit) ---------------------------------------------------------------

CHOICES = ("catch-up", "default", "concede", "unconcede")


def respond_to_offer(
    engine: Engine, user_id: str, code: str, choice: str, semester_name: str | None = None
) -> dict:
    """His answer to an offer, on one course, as one write unit (Write units, D-296).

    - "catch-up" / "default": `strategy` and `strategy_changed_at` = now.
    - "concede": `conceded` = 1, `conceded_at` = now. "unconcede": `conceded` = 0,
      `conceded_at` = NULL - the history stays in each plan's `strategy_snapshot`.
    Refused, writing nothing: an unknown choice (`invalid_value`, the four choices in the fix),
    a course not found (`find_course`'s error), a dropped or unplanned course, or a choice that
    is already the course's state (`already_set`, e.g. catch-up on a course on catch-up).
    No ranking changes (D-299).

    Returns {"course_id", "code", "strategy", "strategy_changed_at", "conceded",
    "conceded_at"} after the write.

    Examples: I3351 + "catch-up" -> strategy catch-up, strategy_changed_at set · I3351 +
    "catch-up" again -> already_set · DHR300 + "concede" -> conceded 1 with a date ·
    DHR300 + "unconcede" -> conceded 0, conceded_at NULL.
    `semester_name` narrows the code when it exists in two semesters, as in every course tool.
    """
    if choice not in CHOICES:  # before the unit: a bad choice opens no transaction
        raise invalid(
            "choice",
            f"{choice!r} is not an answer to an offer",
            f"send one of: {', '.join(CHOICES)}",
        )

    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        row = conn.execute(select(course).where(course.c.id == course_id)).one()
        if not row.planned or row.dropped_at is not None:
            raise StudyError(
                code="not_in_plan",
                message=f"{row.code} is not planned this semester, so it gets no offer",
                fix="answer offers only for the courses the shortfall alert lists",
                field_errors=[{"field": "code", "problem": "dropped or never planned"}],
            )

        stamp = now()
        if choice in ("catch-up", "default"):
            current, changes = row.strategy, {"strategy": choice, "strategy_changed_at": stamp}
        else:
            current = "concede" if row.conceded else "unconcede"
            changes = (
                {"conceded": 1, "conceded_at": stamp}
                if choice == "concede"
                else {"conceded": 0, "conceded_at": None}
            )
        if current == choice:
            raise StudyError(
                code="already_set",
                message=f"{row.code} is already {_state_word(choice)}",
                fix="nothing to do - read the course's state in the shortfall alert",
                field_errors=[{"field": "choice", "problem": f"already {_state_word(choice)}"}],
            )
        conn.execute(update(course).where(course.c.id == course_id).values(changes))
        after = conn.execute(select(course).where(course.c.id == course_id)).one()

    return {
        "course_id": course_id,
        "code": after.code,
        "strategy": after.strategy,
        "strategy_changed_at": after.strategy_changed_at,
        "conceded": bool(after.conceded),
        "conceded_at": after.conceded_at,
    }


def _state_word(choice: str) -> str:
    """How a choice reads as a state, for the already_set message."""
    return {
        "catch-up": "on catch-up",
        "default": "on the default strategy",
        "concede": "conceded",
        "unconcede": "not conceded",
    }[choice]
