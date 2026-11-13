"""Adherence (4.7; D-12, D-22, D-117, D-273 - D-276): of one week's minutes, the share that went
to something the plan in force showed. Read as agreement - his own pick against the plan's
(D-273) - with every off-plan minute sorted by its reason.

Read only. On his live data, the week of 2026-11-02 (Beirut) -> 90 of 200 minutes in-plan, 45%:
the 50 min on Ch3 Transport (2) and the 40 min on Ch5 UML matched shown items; the 60 min
course-level I3303 hour and the 50 min on the "(1)" copy of Ch3 did not, and read `unexplained`.
"""

import datetime

from sqlalchemy import ColumnElement, Connection, or_, select

from studysystem.db.tables import hours_entry, plan_item
from studysystem.services.gate import plan_in_force

# Why an hour went off the plan (D-12, D-274), as `hours_entry.deviation_reason` holds them.
# `study_log_hours` and the timer's stop check a reason against this list (D-276).
DEVIATION_REASONS = (
    "external-deadline",
    "disagreed",
    "too-tired",
    "topic-wrong",
    "backlog",
    "unexplained",
)
# Only this reason leaves the denominator: the server did not know about the deadline, so the
# hour reads as a missing input, not a miss (D-12).
MISSING_INPUT = "external-deadline"
# The keys of the breakdown: every reason but MISSING_INPUT, each always present, 0 when empty,
# so `unexplained` shows as a category even in a week with none (D-12).
DEVIATION_KEYS = tuple(r for r in DEVIATION_REASONS if r != MISSING_INPUT)


def _week_bounds(week_start: datetime.date, tz: datetime.tzinfo | None) -> tuple[str, str]:
    """The week from `week_start` 00:00 to 7 days later 00:00 on the student's clock (`tz`, None =
    this machine's), as the UTC timestamps `occurred_at` is stored in - so a bound and a stored
    start compare as text. Pure.

    2026-11-02 in Beirut (UTC+2 in November) -> ("2026-11-01T22:00:00Z", "2026-11-08T22:00:00Z").
    The week holds an hour started at the first and not one started at the second.
    """
    bounds = []
    for day in (week_start, week_start + datetime.timedelta(days=7)):
        midnight = datetime.datetime.combine(day, datetime.time())
        midnight = midnight.astimezone() if tz is None else midnight.replace(tzinfo=tz)
        bounds.append(midnight.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return bounds[0], bounds[1]


def in_plan(user_id: str) -> ColumnElement[bool]:
    """A yes/no SQL expression about the outer `hours_entry` row: whether the plan in force at its
    start (`plan_in_force`, D-116) holds a `plan_item` that is shown and is about the same thing as
    the hour - the same `topic_id`, `setup_key` or `material_id` (D-22, D-243). The weekly query
    reads it per hour, and `study_log_hours` reads it for the entry it just wrote (D-276).

    One answer per hour, however many items match: a topic shown as review #1 and study #1 of one
    plan is still one yes, so its minutes are summed once (D-117). That is why it is an exists and
    not a join.

    His data (plans are local time):
        Nov 5, 50 min on Ch3 Transport (2), plan of Nov 5 20:18 shows study #1 under it -> yes
        Nov 5, 50 min on Ch3 Transport (1), plan of Nov 4 22:38 shows only "(2)"        -> no
        Nov 3, 60 min on I3303 with no topic, chapter or key (course-level)             -> no
        Oct 9, 20 min on Protocol layers, shown in the 18:08 plan                       -> yes
        an hour on a topic the plan in force ranked but did not show                    -> no
        an hour before any plan existed (no plan in force)                              -> no
    """

    query = (
        select(plan_item)
        .where(
            plan_item.c.plan_id.in_(plan_in_force(user_id)),
            plan_item.c.shown == 1,
            or_(
                plan_item.c.topic_id == hours_entry.c.topic_id,
                plan_item.c.setup_key == hours_entry.c.setup_key,
                plan_item.c.material_id == hours_entry.c.material_id,
            ),
        )
        .exists()
    )
    return query


def weekly_adherence(
    conn: Connection, user_id: str, week_start: datetime.date, tz: datetime.tzinfo | None = None
) -> dict:
    """One week of `user_id`'s hours, scored: the week from `week_start` (a Monday) 00:00 to the
    next Monday 00:00 on the student's clock (`tz`, None = this machine's). Reads on `conn`, so
    `GET /state` scores the week inside its one read unit, with every other section.

    Returns {"week_start", "minutes", "in_plan", "missing_input", "adherence", "deviations"}:
    - minutes: every live (not voided) hour started in the week
    - in_plan: the minutes of hours `in_plan` says yes to - any reason they carry is ignored
    - missing_input: off-plan minutes whose reason is `external-deadline`
    - adherence: in_plan / (minutes - missing_input); None when that bottom number is 0 - a week
      with no hours, or only missing inputs, is unknown, never 0% (unknown stays unknown)
    - deviations: the other off-plan minutes by reason, one key per DEVIATION_KEYS, 0 when empty;
      an off-plan hour with no reason counts as `unexplained` (D-276)

    His data, Beirut, every hour reasonless:
        2026-11-02 -> {"week_start": "2026-11-02", "minutes": 200, "in_plan": 90,
                       "missing_input": 0, "adherence": 0.45,
                       "deviations": {"disagreed": 0, "too-tired": 0, "topic-wrong": 0,
                                      "backlog": 0, "unexplained": 110}}
        2026-10-12 -> minutes 200, in_plan 0, adherence 0.0, unexplained 200 (five course-level
                      I3306 hours)
        2026-10-26 -> minutes 0, in_plan 0, adherence None (no hours)
        2026-10-12, if all five I3306 hours said external-deadline -> missing_input 200,
                      adherence None, every deviation 0 (4.7's Done-when)
        an hour at Sunday 23:30 local (21:30Z) belongs to that week; one at Monday 00:30 local
        (Sunday 22:30Z) belongs to the next
    """
    start, end = _week_bounds(week_start, tz)
    query = select(
        hours_entry.c.minutes.label("minutes"),
        hours_entry.c.deviation_reason.label("reason"),
        in_plan(user_id).label("in_plan"),
    ).where(
        hours_entry.c.user_id == user_id,
        hours_entry.c.voided_at.is_(None),
        hours_entry.c.occurred_at >= start,
        hours_entry.c.occurred_at < end,
    )
    rows = conn.execute(query).all()

    minutes = planned = missing = 0
    deviations = dict.fromkeys(DEVIATION_KEYS, 0)
    for row in rows:
        minutes += row.minutes
        if row.in_plan:
            planned += row.minutes
        elif row.reason == MISSING_INPUT:
            missing += row.minutes
        else:
            deviations[row.reason or "unexplained"] += row.minutes

    bottom = minutes - missing
    return {
        "week_start": week_start.isoformat(),
        "minutes": minutes,
        "in_plan": planned,
        "missing_input": missing,
        "adherence": planned / bottom if bottom else None,
        "deviations": deviations,
    }
