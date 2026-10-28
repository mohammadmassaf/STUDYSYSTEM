"""Feedback capture and directives (2.9; D-24, D-213): what he says about a generated item, and
the standing instruction it may become.

Three kinds of feedback, three destinations (D-24). An item defect is an `item_rejection` and
retires the item; his own format preference or a correction to the model's output is a
`generation_directive`. A claim about the instructor is an observation - that is
`study_add_instructor_statement`, never this module, so "too easy" cannot reach the profile.

`study_submit_feedback` is one write unit: the rejection, the item's state and the drafted
directive (physical-schema.md, *Write units*). A directive is drafted `proposed` and enters a
payload only after `study_confirm_directive` (D-24) - one row, no unit entry.
"""

from sqlalchemy import Connection, Engine, and_, exists, insert, or_, select, update

from studysystem.db.tables import (
    course,
    generation_directive,
    item_rejection,
    practice_item,
    topic,
)
from studysystem.errors import StudyError
from studysystem.services import values
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_course
from studysystem.services.units import write_unit

KINDS = ("item-defect", "format-preference", "generator-correction")
# `wrong-answer-key` waits for 4.2, which voids the attempts against the item (D-213).
REASONS = ("off-syllabus", "wrong-topic", "duplicate", "unclear", "wrong-difficulty")
SCOPES = ("topic", "course", "all")
DIRECTIVE_KINDS = ("format-preference", "generator-correction")


def _not_found(field: str, problem: str, fix: str) -> StudyError:
    return StudyError(
        code="not_found",
        message=f"{field}: {problem}",
        fix=fix,
        field_errors=[{"field": field, "problem": problem}],
    )


def _check_inputs(
    kind: str,
    practice_item_id: str | None,
    reason: str | None,
    directive_text: str | None,
    scope: str | None,
    code: str | None,
    directive_kind: str | None,
) -> tuple[str, str | None, str | None, str, str]:
    """Every refusal that needs no database, before the unit opens. Returns kind, reason,
    directive text, scope and directive kind in their stored form, defaults filled in."""
    kind = kind.strip().lower() if isinstance(kind, str) else kind
    if kind not in KINDS:
        raise values.invalid(
            "kind",
            f"{kind!r} is not a kind of feedback",
            f"use one of {', '.join(KINDS)} - a claim about the instructor is "
            "study_add_instructor_statement",
        )
    if directive_text is not None:
        directive_text = values.text("directive_text", directive_text)
    if scope is not None:
        scope = values.choice("scope", scope, SCOPES)
    if directive_kind is not None:
        directive_kind = values.choice("directive_kind", directive_kind, DIRECTIVE_KINDS)

    if kind == "item-defect":
        if practice_item_id is None:
            raise values.invalid(
                "practice_item_id",
                "an item defect needs the item",
                "send the id of the item the feedback is about",
            )
        if isinstance(reason, str) and reason.strip().lower() == "wrong-answer-key":
            raise values.invalid(
                "reason",
                "a wrong answer key is not taken yet - its attempts must be voided first (4.2)",
                f"use one of {', '.join(REASONS)}, or wait for study_reject_item",
            )
        reason = values.choice("reason", reason, REASONS)
        # a note on a bad item is usually about the model's output; "I prefer short MCQ" is
        # about him, so the call may say so (D-213)
        return (
            kind,
            reason,
            directive_text,
            scope or "topic",
            directive_kind or "generator-correction",
        )

    if practice_item_id is not None:
        raise values.invalid(
            "practice_item_id",
            f"{kind} is about generation in general, not one item",
            "leave practice_item_id out, or send kind item-defect",
        )
    if directive_text is None:
        raise values.invalid(
            "directive_text",
            f"{kind} is the directive itself, and none was sent",
            "send the instruction to carry into future generation, in one sentence",
        )
    if directive_kind is not None and directive_kind != kind:
        raise values.invalid(
            "directive_kind",
            f"kind {kind} already is the directive's kind",
            "leave directive_kind out - it is only for a directive drafted from an item defect",
        )
    if scope == "topic":
        raise values.invalid(
            "scope", f"{kind} names no item, so it has no topic", "use course or all"
        )
    scope = scope or "course"
    if code is None and scope == "course":  # an all-courses directive stores no course
        raise values.invalid(
            "code",
            f"{kind} for one course needs its course",
            "send the course code, e.g. I3302, or scope all for every course",
        )
    return kind, None, directive_text, scope, kind


def submit_feedback(
    engine: Engine,
    user_id: str,
    kind: str,
    practice_item_id: str | None = None,
    reason: str | None = None,
    note: str | None = None,
    directive_text: str | None = None,
    scope: str | None = None,
    code: str | None = None,
    semester_name: str | None = None,
    directive_kind: str | None = None,
) -> dict:
    """File one piece of feedback (D-213), as one write unit.

    `item-defect`: an `item_rejection` against `practice_item_id` with `reason`, the item's
    `state` -> `rejected`, and - with `directive_text` - a `proposed` directive drafted from it,
    a `generator-correction` unless `directive_kind` says `format-preference`. `scope` defaults
    to `topic` (the item's course and topic). The item may be `live` or `needs-review`, or
    `rejected` with no rejection row yet - D-136's four, retired before this tool existed.
    `format-preference` / `generator-correction`: no item - only the `proposed` directive, from
    `directive_text`, for the course named by `code`. `scope` defaults to `course`; with `all`
    no course is stored, so `code` is not needed.

    Refuses `invalid_value` for a bad kind, reason (`wrong-answer-key` until 4.2), scope or
    directive kind (on an item-less call, one that differs from `kind`), an empty directive text,
    or a missing input its kind needs (`code` only for scope `course`); `not_found` for an item
    that is not this user's or already has its rejection.

    Returns {"rejection_id" or None, "directive": {"id", "kind", "text", "scope", "status"}
    or None}.
    """
    kind, reason, directive_text, scope, directive_kind = _check_inputs(
        kind, practice_item_id, reason, directive_text, scope, code, directive_kind
    )
    note = (note.strip() or None) if note is not None else None  # unknown stays NULL
    rej_id = None
    topic_id = None
    course_id = None

    with write_unit(engine) as conn:
        if kind == "item-defect":
            not_yet_filed = ~exists().where(item_rejection.c.practice_item_id == practice_item.c.id)
            item = conn.execute(
                select(practice_item.c.topic_id, topic.c.course_id)
                .join(topic, topic.c.id == practice_item.c.topic_id)
                .where(
                    practice_item.c.id == practice_item_id,
                    practice_item.c.user_id == user_id,
                    or_(
                        practice_item.c.state.in_(("live", "needs-review")),
                        and_(practice_item.c.state == "rejected", not_yet_filed),
                    ),
                )
            ).one_or_none()
            if item is None:
                raise _not_found(
                    "practice_item_id",
                    "no item of yours with this id is open to rejection - only a live or "
                    "needs-review item, or a rejected one not yet filed",
                    "check the item's id; a superseded or already filed item stays as it is",
                )
            rej_id = new_id()
            conn.execute(
                insert(item_rejection).values(
                    id=rej_id,
                    user_id=user_id,
                    practice_item_id=practice_item_id,
                    reason=reason,
                    note=note,
                    created_at=now(),
                )
            )
            conn.execute(
                update(practice_item)
                .where(practice_item.c.id == practice_item_id)
                .values(state="rejected")
            )
            topic_id, course_id = item.topic_id, item.course_id
        elif scope == "course":
            assert code is not None  # _check_inputs refused a one-course directive with none
            course_id = find_course(conn, user_id, code, semester_name)

        directive = None
        if directive_text is not None:
            if scope != "topic":
                topic_id = None
            if scope == "all":
                course_id = None
            directive = {
                "id": new_id(),
                "kind": directive_kind,
                "text": directive_text,
                "scope": scope,
                "status": "proposed",
            }
            conn.execute(
                insert(generation_directive).values(
                    id=directive["id"],
                    user_id=user_id,
                    course_id=course_id,
                    topic_id=topic_id,
                    kind=directive["kind"],
                    text=directive["text"],
                    status="proposed",
                    source_rejection_id=rej_id,
                    created_at=now(),
                )
            )

    return {"rejection_id": rej_id, "directive": directive}


def confirm_directive(engine: Engine, user_id: str, directive_id: str) -> dict:
    """His explicit yes (D-24): a `proposed` directive becomes `active`, stamped `confirmed_at`.
    From then on it rides every note and practice payload in its scope (2.10, D-214).

    Refuses `not_found` for an id that is not this user's `proposed` directive - unknown,
    another user's, or already confirmed.

    Returns {"id", "kind", "text", "status", "confirmed_at"}.
    """
    confirmed_at = now()
    with write_unit(engine) as conn:
        row = conn.execute(
            select(generation_directive.c.kind, generation_directive.c.text).where(
                generation_directive.c.id == directive_id,
                generation_directive.c.user_id == user_id,
                generation_directive.c.status == "proposed",
            )
        ).one_or_none()
        if row is None:
            raise _not_found(
                "directive_id",
                "no proposed directive of yours has this id - it may already be confirmed",
                "use the id study_submit_feedback returned",
            )
        conn.execute(
            update(generation_directive)
            .where(generation_directive.c.id == directive_id)
            .values(status="active", confirmed_at=confirmed_at)
        )

    return {
        "id": directive_id,
        "kind": row.kind,
        "text": row.text,
        "status": "active",
        "confirmed_at": confirmed_at,
    }


def active_directives(conn: Connection, user_id: str, course_id: str) -> list[dict]:
    """The directives a note or practice task on `course_id` carries (2.10, D-24, D-214): this
    user's `active` ones whose course is `course_id` or NULL (every course). A topic directive
    rides whether or not the material covers its topic - a practice task writes on every topic
    of the course - but only while that topic is `active`, since the payload lists no other.
    Oldest confirmation first.

    Each entry is {"text", "scope", "applies_to"}: `text` exactly as stored, `scope` the same
    `topic` | `course` | `all` that study_submit_feedback takes, and `applies_to` the topic's
    name, the course's code, or None for `all`.
    """
    # outer joins: a directive for every course has no course row, one for the course no topic
    rows = conn.execute(
        select(generation_directive.c.text, topic.c.name, course.c.code)
        .outerjoin(course, course.c.id == generation_directive.c.course_id)
        .outerjoin(topic, topic.c.id == generation_directive.c.topic_id)
        .where(
            generation_directive.c.user_id == user_id,
            generation_directive.c.status == "active",
            or_(
                generation_directive.c.course_id == course_id,
                generation_directive.c.course_id.is_(None),
            ),
            or_(generation_directive.c.topic_id.is_(None), topic.c.status == "active"),
        )
        .order_by(generation_directive.c.confirmed_at, generation_directive.c.id)
    ).all()
    result = []
    for row in rows:
        if row.name is not None:
            scope, applies_to = "topic", row.name
        elif row.code is not None:
            scope, applies_to = "course", row.code
        else:
            scope, applies_to = "all", None
        result.append({"text": row.text, "scope": scope, "applies_to": applies_to})

    return result
