"""The dashboard's read model (3.2; D-19, D-22, D-163): `GET /state` as one blob, one builder per
top-level section, each with its own schema. Every builder reads inside one read unit, so the blob
comes from one moment - and nothing is written, not even a plan: the plan section is the latest
stored one.

The tier rule of physical-schema.md holds on the way out. A field that can be null travels as a
pair, `x` and `x_tier` (`declared | inferred | unknown`), and `unknown` <=> null - so a null is
never bare and an unknown is never 0. Each section's schema checks the rule, and every section is
checked before it leaves.
"""

import datetime
import json
from collections.abc import Callable

from jsonschema import Draft202012Validator
from sqlalchemy import Connection, Engine, func, select

from studysystem.db.tables import (
    assessment,
    assessment_slot,
    capacity,
    capacity_suggestion,
    class_meeting,
    course,
    exam_profile,
    hours_entry,
    plan,
    plan_item,
    topic,
)
from studysystem.services.ids import now
from studysystem.services.topics import TAGGABLE as LIVE_TOPICS  # the live statuses
from studysystem.services.units import read_unit

TIERS = ["declared", "inferred", "unknown"]

# --- schema pieces ------------------------------------------------------------

ID = {"type": "string", "minLength": 26, "maxLength": 26}
TEXT = {"type": "string"}
NUMBER = {"type": "number"}
COUNT = {"type": "integer", "minimum": 0}
BOOL = {"type": "boolean"}
DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
TIME = {"type": "string", "pattern": r"^\d{2}:\d{2}$"}
STAMP = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"}


def _record(plain: dict, tiered: dict | None = None) -> dict:
    """An object schema: every field required, no others. `plain` fields are never null. Each
    `tiered` field may be null, travels with `<field>_tier`, and the pair obeys the tier rule."""
    tiered = tiered or {}
    properties = dict(plain)
    rules = []
    for name, schema in tiered.items():
        properties[name] = {"anyOf": [schema, {"type": "null"}]}
        properties[f"{name}_tier"] = {"enum": TIERS}
        rules.append(
            {
                "if": {"properties": {name: {"type": "null"}}},
                "then": {"properties": {f"{name}_tier": {"const": "unknown"}}},
                "else": {"properties": {f"{name}_tier": {"enum": ["declared", "inferred"]}}},
            }
        )
    schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    if rules:
        schema["allOf"] = rules
    return schema


def _list(items: dict) -> dict:
    return {"type": "array", "items": items}


def _pair(name: str, value, tier: str) -> dict:
    """`x` and `x_tier`: `tier` when the value is set, else unknown. No default - each caller
    names the tier its value has, so an unconfirmed one cannot pass as declared."""
    return {name: value, f"{name}_tier": tier if value is not None else "unknown"}


# --- semester -----------------------------------------------------------------

SEMESTER_SCHEMA = _record({"end_approx": BOOL}, {"name": TEXT, "start": DATE, "end": DATE})


def _semester(conn: Connection, user_id: str, today: datetime.date) -> dict:
    """The semester of the course added last. Its span runs from the earliest start any of its
    courses declares to the latest end."""
    name = conn.execute(
        select(course.c.semester_name)
        .where(course.c.user_id == user_id)
        .order_by(course.c.id.desc())  # a ULID sorts by when it was made
        .limit(1)
    ).scalar()
    rows = conn.execute(
        select(course.c.semester_start, course.c.semester_end, course.c.semester_end_approx).where(
            course.c.user_id == user_id,
            func.lower(course.c.semester_name) == func.lower(name or ""),
        )
    ).all()
    starts = [row.semester_start for row in rows if row.semester_start is not None]
    ends = [row.semester_end for row in rows if row.semester_end is not None]
    end = max(ends, default=None)
    return {
        **_pair("name", name, "declared"),
        **_pair("start", min(starts, default=None), "declared"),
        **_pair("end", end, "declared"),
        "end_approx": any(
            row.semester_end_approx for row in rows if end is not None and row.semester_end == end
        ),
    }


# --- capacity -----------------------------------------------------------------

CAPACITY_SCHEMA = _record(
    {}, {"declared_hours_per_week": NUMBER, "from_date": DATE, "suggested_from_timetable": NUMBER}
)


def _capacity(conn: Connection, user_id: str, today: datetime.date) -> dict:
    """The capacity in force today (the latest `from_date <= today`, ties to the later id), and a
    pending timetable suggestion beside it - never in its place (D-19)."""
    declared = conn.execute(
        select(capacity.c.hours_per_week, capacity.c.from_date)
        .where(capacity.c.user_id == user_id, capacity.c.from_date <= today.isoformat())
        .order_by(capacity.c.from_date.desc(), capacity.c.id.desc())
        .limit(1)
    ).first()
    suggested = conn.execute(
        select(capacity_suggestion.c.hours_per_week)
        .where(capacity_suggestion.c.user_id == user_id, capacity_suggestion.c.status == "pending")
        .order_by(capacity_suggestion.c.id.desc())
        .limit(1)
    ).scalar()
    return {
        **_pair(
            "declared_hours_per_week", declared.hours_per_week if declared else None, "declared"
        ),
        **_pair("from_date", declared.from_date if declared else None, "declared"),
        **_pair("suggested_from_timetable", suggested, "inferred"),
    }


# --- timetable ----------------------------------------------------------------

MEETING_SCHEMA = _record(
    {
        "meeting_id": ID,
        "course_id": ID,
        "code": TEXT,
        "weekday": {"type": "integer", "minimum": 1, "maximum": 7},  # ISO, 1 = Monday
        "start": TIME,
        "end": TIME,
        "kind": {"enum": ["lecture", "lab", "tutorial"]},
        "tier": {"enum": ["declared", "inferred"]},  # inferred: read off an image, unconfirmed
    },
    {"room": TEXT},
)
TIMETABLE_SCHEMA = _record({}, {"meetings": _list(MEETING_SCHEMA)})


def _timetable(conn: Connection, user_id: str, today: datetime.date) -> dict:
    """His week of classes. No meetings entered is unknown, not an empty week; the week is
    inferred while any meeting in it is."""
    rows = conn.execute(
        select(class_meeting, course.c.code)
        .join_from(class_meeting, course, course.c.id == class_meeting.c.course_id)
        .where(class_meeting.c.user_id == user_id)
        .order_by(class_meeting.c.weekday, class_meeting.c.start_time, course.c.code)
    ).all()
    meetings = [
        {
            "meeting_id": row.id,
            "course_id": row.course_id,
            "code": row.code,
            "weekday": row.weekday,
            "start": row.start_time,
            "end": row.end_time,
            **_pair("room", row.room, row.tier),  # a room read off an image is unconfirmed too
            "kind": row.kind,
            "tier": row.tier,
        }
        for row in rows
    ]
    if not meetings:
        return _pair("meetings", None, "declared")
    inferred = any(meeting["tier"] == "inferred" for meeting in meetings)
    return _pair("meetings", meetings, "inferred" if inferred else "declared")


# --- assessment slots ---------------------------------------------------------

PROFILE_SCHEMA = _record(
    {
        "version": {"type": "integer", "minimum": 1},
        "derived_at": STAMP,
        "provisional": _record({"syllabus": BOOL, "instructor": BOOL, "format": BOOL}),
    }
)
ASSESSMENT_SCHEMA = _record(
    {
        "assessment_id": ID,
        "slot_id": ID,
        "course_id": ID,
        "code": TEXT,
        "name": TEXT,
        "kind": {"enum": ["exam", "project", "lab"]},
        "date_approx": BOOL,
        "status": {"enum": ["upcoming", "sat", "graded"]},
    },
    {
        "session_type": {"enum": ["first", "second"]},
        "date": DATE,
        "time": TIME,
        "room": TEXT,
        "weight": NUMBER,
        "mark": NUMBER,
        "profile": PROFILE_SCHEMA,
    },
)
ASSESSMENT_SLOTS_SCHEMA = _list(ASSESSMENT_SCHEMA)


def _assessment_slots(conn: Connection, user_id: str, today: datetime.date) -> list[dict]:
    """Every assessment, dated first and soonest first. Each carries its slot's newest exam
    profile, whose provisional flags say which claim class still rests on too little evidence;
    no profile yet is unknown (a cold start)."""
    newest = (
        select(exam_profile.c.slot_id, func.max(exam_profile.c.version).label("version"))
        .group_by(exam_profile.c.slot_id)
        .subquery()
    )
    profiles = {
        row.slot_id: {
            "version": row.version,
            "derived_at": row.derived_at,
            "provisional": {
                "syllabus": bool(row.provisional_syllabus),
                "instructor": bool(row.provisional_instructor),
                "format": bool(row.provisional_format),
            },
        }
        for row in conn.execute(
            select(exam_profile)
            .join_from(
                exam_profile,
                newest,
                (newest.c.slot_id == exam_profile.c.slot_id)
                & (newest.c.version == exam_profile.c.version),
            )
            .where(exam_profile.c.owner_id == user_id)
        )
    }
    rows = conn.execute(
        select(
            assessment,
            assessment_slot.c.name,
            assessment_slot.c.kind,
            assessment_slot.c.course_id,
            course.c.code,
        )
        .join_from(assessment, assessment_slot, assessment_slot.c.id == assessment.c.slot_id)
        .join(course, course.c.id == assessment_slot.c.course_id)
        .where(assessment.c.user_id == user_id)
        .order_by(
            assessment.c.date.is_(None), assessment.c.date, course.c.code, assessment_slot.c.name
        )
    ).all()
    return [
        {
            "assessment_id": row.id,
            "slot_id": row.slot_id,
            "course_id": row.course_id,
            "code": row.code,
            "name": row.name,
            "kind": row.kind,
            **_pair("session_type", row.session_type, "declared"),
            **_pair("date", row.date, "declared"),
            "date_approx": bool(row.date_approx),
            **_pair("time", row.time, "declared"),
            **_pair("room", row.room, "declared"),
            "weight": row.weight,
            "weight_tier": row.weight_tier,
            "status": row.status,
            **_pair("mark", row.mark, "declared"),
            **_pair("profile", profiles.get(row.slot_id), "inferred"),
        }
        for row in rows
    ]


# --- plan ---------------------------------------------------------------------

_ITEM = {"rank": {"type": "integer", "minimum": 1}, "course_id": ID, "code": TEXT, "reason": TEXT}
REVIEW_ITEM_SCHEMA = _record({**_ITEM, "topic_id": ID, "topic_name": TEXT, "score": NUMBER})
STUDY_ITEM_SCHEMA = _record(
    {**_ITEM, "topic_id": ID, "topic_name": TEXT, "score": NUMBER},
    {"estimated_minutes": {"type": "integer", "minimum": 1}},
)
SETUP_ITEM_SCHEMA = _record(
    {**_ITEM, "setup_kind": TEXT, "setup_key": TEXT}, {"marks_unlocked_estimate": NUMBER}
)


def _lane(item: dict) -> dict:
    return _record({"items": _list(item), "hidden": COUNT})


CURRENT_PLAN_SCHEMA = _record(
    {
        "plan_id": ID,
        "created_at": STAMP,
        "shown_n": {"type": "integer", "minimum": 1},
        "strategy_by_course": {
            "type": "object",
            "additionalProperties": {"enum": ["default", "catch-up"]},
        },
        "lanes": _record(
            {
                "review": _lane(REVIEW_ITEM_SCHEMA),
                "study": _lane(STUDY_ITEM_SCHEMA),
                "setup": _lane(SETUP_ITEM_SCHEMA),
            }
        ),
    }
)
PLAN_SCHEMA = _record({}, {"current": CURRENT_PLAN_SCHEMA})


def _plan_item(row) -> dict:
    """What the dashboard reads of a stored item. Minutes and marks are the scheduler's
    estimates - the flat 60 minutes of D-83 - so their tier is inferred."""
    item = {"rank": row.rank, "course_id": row.course_id, "code": row.code, "reason": row.reason}
    if row.lane == "setup":
        return {
            **item,
            "setup_kind": row.setup_kind,
            "setup_key": row.setup_key,
            **_pair("marks_unlocked_estimate", row.marks_unlocked_estimate, "inferred"),
        }
    item.update(topic_id=row.topic_id, topic_name=row.topic_name, score=row.score)
    if row.lane == "study":
        item.update(_pair("estimated_minutes", row.estimated_minutes, "inferred"))
    return item


def _plan(conn: Connection, user_id: str, today: datetime.date) -> dict:
    """The latest stored all-courses plan - what the host was last shown (D-163). Reading never
    makes one: a GET that wrote a plan would move the plan in force on every refresh. No plan
    yet is unknown."""
    latest = conn.execute(
        select(plan)
        .where(plan.c.user_id == user_id, plan.c.scope == "all-courses")
        .order_by(plan.c.id.desc())
        .limit(1)
    ).first()
    if latest is None:
        return _pair("current", None, "inferred")
    rows = conn.execute(
        select(plan_item, course.c.code, topic.c.name.label("topic_name"))
        .join_from(plan_item, course, course.c.id == plan_item.c.course_id)
        .outerjoin(topic, topic.c.id == plan_item.c.topic_id)
        .where(plan_item.c.plan_id == latest.id)
        .order_by(plan_item.c.lane, plan_item.c.rank)
    ).all()
    lanes = {
        lane: {
            "items": [_plan_item(row) for row in rows if row.lane == lane and row.shown],
            "hidden": sum(1 for row in rows if row.lane == lane and not row.shown),
        }
        for lane in ("review", "study", "setup")
    }
    snapshot = json.loads(latest.strategy_snapshot)
    current = {
        "plan_id": latest.id,
        "created_at": latest.created_at,
        "shown_n": latest.shown_n,
        "strategy_by_course": {key: value["strategy"] for key, value in snapshot.items()},
        "lanes": lanes,
    }
    return _pair("current", current, "inferred")


# --- courses ------------------------------------------------------------------

TOPIC_SCHEMA = _record({"topic_id": ID, "name": TEXT, "status": {"enum": list(LIVE_TOPICS)}})
COURSE_SCHEMA = _record(
    {
        "course_id": ID,
        "code": TEXT,
        "name": TEXT,
        "semester_name": TEXT,
        "strategy": {"enum": ["default", "catch-up"]},
        "conceded": BOOL,
        "minutes_logged": COUNT,  # not voided; hours are explicit, so 0 is a real 0
        "topics": _list(TOPIC_SCHEMA),
    },
    {"instructor": TEXT, "credits": NUMBER, "target_grade": NUMBER},
)
COURSES_SCHEMA = _list(COURSE_SCHEMA)


def _courses(conn: Connection, user_id: str, today: datetime.date) -> list[dict]:
    """Every course with its declared inputs and their tiers, the minutes logged on it, and its
    live topics."""
    minutes = {
        row.course_id: row.minutes
        for row in conn.execute(
            select(hours_entry.c.course_id, func.sum(hours_entry.c.minutes).label("minutes"))
            .where(hours_entry.c.user_id == user_id, hours_entry.c.voided_at.is_(None))
            .group_by(hours_entry.c.course_id)
        )
    }
    topics: dict[str, list[dict]] = {}
    for row in conn.execute(
        select(topic.c.id, topic.c.course_id, topic.c.name, topic.c.status)
        .where(topic.c.user_id == user_id, topic.c.status.in_(LIVE_TOPICS))
        .order_by(topic.c.name)
    ):
        topics.setdefault(row.course_id, []).append(
            {"topic_id": row.id, "name": row.name, "status": row.status}
        )
    rows = conn.execute(select(course).where(course.c.user_id == user_id).order_by(course.c.code))
    return [
        {
            "course_id": row.id,
            "code": row.code,
            "name": row.name,
            "semester_name": row.semester_name,
            "instructor": row.instructor,
            "instructor_tier": row.instructor_tier,
            "credits": row.credits,
            "credits_tier": row.credits_tier,
            **_pair("target_grade", row.target_grade, "declared"),
            "strategy": row.strategy,
            "conceded": bool(row.conceded),
            "minutes_logged": int(minutes.get(row.id, 0)),
            "topics": topics.get(row.id, []),
        }
        for row in rows
    ]


# --- the blob -----------------------------------------------------------------

Builder = Callable[[Connection, str, datetime.date], object]

# One builder and one schema per top-level section (D-22), in the order the blob sends them.
# metrics, shortfall and quality join when their tasks build them (D-163).
SECTIONS: dict[str, tuple[Builder, dict]] = {
    "semester": (_semester, SEMESTER_SCHEMA),
    "capacity": (_capacity, CAPACITY_SCHEMA),
    "timetable": (_timetable, TIMETABLE_SCHEMA),
    "assessment_slots": (_assessment_slots, ASSESSMENT_SLOTS_SCHEMA),
    "plan": (_plan, PLAN_SCHEMA),
    "courses": (_courses, COURSES_SCHEMA),
}
CHECKS = {name: Draft202012Validator(schema) for name, (_, schema) in SECTIONS.items()}


def get_state(engine: Engine, user_id: str, today: datetime.date, server_version: str) -> dict:
    """The whole blob, every section from one read. Each section is checked against its schema
    before it leaves: a builder that breaks its contract fails here, loudly, rather than
    showing the dashboard a bare null."""
    with read_unit(engine) as conn:
        sections = {name: build(conn, user_id, today) for name, (build, _) in SECTIONS.items()}
    for name, section in sections.items():
        CHECKS[name].validate(section)
    return {"server_version": server_version, "generated_at": now(), **sections}
