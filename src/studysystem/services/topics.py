"""Topics and coverage (1.12; D-13, D-22, D-51, D-60 - D-64): a transcription proposes topics, and
he accepts or declines each one. A decline maps every item of the topic to a target (D-61 -
D-63). A coverage edge `topic -> assessment` exists only for a non-proposed topic, and is only
ever inserted when missing - never moved (D-60).

`study_confirm_topic_proposal` is one write unit per topic (physical-schema.md, *Write units*).
`study_get_topics` only reads (D-64).
"""

from collections import Counter

from sqlalchemy import Connection, Engine, func, insert, select, update

from studysystem.db.tables import assessment, coverage, past_exam, practice_item, topic
from studysystem.errors import StudyError
from studysystem.services import values
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_course
from studysystem.services.units import write_unit

# Topics a transcribed item may be tagged to: live ones, `proposed` included (1.10 accepts a
# proposal). `superseded` and `declined` topics are retired and never offered.
TAGGABLE = ("proposed", "active", "unexamined")


def is_live_topic(conn: Connection, user_id: str, course_id: str, topic_id: str) -> bool:
    """Whether `topic_id` is a live topic (TAGGABLE) of this user's course."""
    found = conn.execute(
        select(topic.c.id).where(
            topic.c.id == topic_id,
            topic.c.user_id == user_id,
            topic.c.course_id == course_id,
            topic.c.status.in_(TAGGABLE),
        )
    ).scalar_one_or_none()
    return found is not None


def topic_for(conn: Connection, user_id: str, course_id: str, ref: dict) -> str:
    """The topic id an item is tagged to (D-51)."""

    if "id" in ref:
        return ref["id"]
    name = ref["proposed_name"].strip()
    topic_id = conn.execute(
        select(topic.c.id).where(
            topic.c.user_id == user_id,
            topic.c.course_id == course_id,
            func.lower(topic.c.name) == func.lower(name),
            topic.c.status.in_(TAGGABLE),
        )
    ).scalar_one_or_none()
    if topic_id is not None:
        return topic_id
    topic_id = new_id()

    conn.execute(
        insert(topic).values(
            id=topic_id,
            user_id=user_id,
            course_id=course_id,
            name=name,
            status="proposed",
            proposed_by="profile",
            status_changed_at=now(),
            created_at=now(),
        )
    )
    return topic_id


def edge_if_missing(conn: Connection, user_id: str, topic_id: str, assessment_id: str) -> None:
    """Insert one `inferred`, unconfirmed coverage edge `topic_id -> assessment_id`, unless that
    pair already has one (D-60). Look first: a refused duplicate would abort the whole unit on
    Postgres, and `BEGIN IMMEDIATE` keeps any other writer out between the look and the insert."""
    found = conn.execute(
        select(coverage.c.id).where(
            coverage.c.topic_id == topic_id, coverage.c.assessment_id == assessment_id
        )
    ).scalar_one_or_none()
    if found is not None:
        return
    conn.execute(
        insert(coverage).values(
            id=new_id(),
            user_id=user_id,
            topic_id=topic_id,
            assessment_id=assessment_id,
            tier="inferred",
            created_at=now(),
        )
    )


def exam_assessments(conn: Connection, topic_id: str) -> list[str]:
    """The assessment ids the topic is examined in: its past-exam items -> their paper -> the
    paper's slot -> the slot's assessment(s). Empty when the topic has no past-exam item."""
    rows = (
        conn.execute(
            select(assessment.c.id)
            .join_from(practice_item, past_exam, practice_item.c.past_exam_id == past_exam.c.id)
            .join(assessment, assessment.c.slot_id == past_exam.c.slot_id)
            .where(practice_item.c.topic_id == topic_id)
            .distinct()
            .order_by(assessment.c.id)
        )
        .scalars()
        .all()
    )

    return list(rows)


def get_topics(engine: Engine, user_id: str, code: str, semester_name: str | None = None) -> dict:
    """Every live topic of the course, each with its status and its items (id, paper date,
    position, marks, the full question text) (D-64, D-66)."""
    with engine.connect() as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        topics = conn.execute(
            select(topic.c.id, topic.c.name, topic.c.status)
            .where(
                topic.c.user_id == user_id,
                topic.c.course_id == course_id,
                topic.c.status.in_(TAGGABLE),
            )
            .order_by(topic.c.id)
        ).all()
        result = {"course": code, "topics": []}
        for row in topics:
            items = conn.execute(
                select(
                    practice_item.c.id,
                    past_exam.c.session_date,
                    practice_item.c.position,
                    practice_item.c.marks,
                    practice_item.c.question,
                )
                .join_from(
                    practice_item,
                    past_exam,
                    practice_item.c.past_exam_id == past_exam.c.id,
                    isouter=True,
                )
                .where(practice_item.c.topic_id == row.id)
                .order_by(
                    past_exam.c.session_date.nulls_last(),
                    practice_item.c.position.nulls_last(),
                    practice_item.c.id,
                )
            ).all()
            result["topics"].append(
                {
                    "id": row.id,
                    "name": row.name,
                    "status": row.status,
                    "items": [
                        {
                            "id": item.id,
                            "paper_date": item.session_date,
                            "position": item.position,
                            "marks": item.marks,
                            "question": item.question,
                        }
                        for item in items
                    ],
                }
            )
        return result


def confirm_topic_proposal(
    engine: Engine,
    user_id: str,
    topic_id: str,
    decision: str,
    retag: list[dict] | None = None,
) -> dict:
    """Accept or decline one `proposed` topic, as one write unit.

    accept: status -> `active`; then one edge for each id `exam_assessments` returns (D-60).
      Sending `retag` with an accept is an error - the host probably meant decline.
    decline: status -> `declined`; `retag` is `[{item_id, topic: {id} | {proposed_name}}]`,
      naming every item of the topic exactly once; each `active` target gets its edge (D-61 - D-63).
    """
    decision = values.choice("decision", decision, ("accept", "decline"))
    if decision == "accept" and retag is not None:
        raise StudyError(
            code="invalid_value",
            message="an accept keeps the topic's items where they are, so it takes no retag",
            fix="to move the items, send decision 'decline' with this retag; to accept, drop retag",
            field_errors=[{"field": "retag", "problem": "sent with decision 'accept'"}],
        )
    if decision == "decline" and retag is None:
        raise StudyError(
            code="invalid_value",
            message="a decline must say where each of the topic's items goes (D-61)",
            fix="send retag: one {item_id, topic} per item - study_get_topics lists them",
            field_errors=[{"field": "retag", "problem": "missing on a decline"}],
        )
    if decision == "decline":
        shape = _retag_shape_errors(retag)
        if shape:
            raise StudyError(
                code="invalid_value",
                message="some retag entries are not {item_id, topic: {id} | {proposed_name}}",
                fix="send each entry as {item_id, topic: {id} or {proposed_name}}",
                field_errors=shape,
            )

    with write_unit(engine) as conn:
        found = conn.execute(
            select(topic).where(topic.c.user_id == user_id, topic.c.id == topic_id)
        ).one_or_none()
        if not found:
            raise StudyError(
                code="not_found",
                message=f"no topic {topic_id!r}",
                fix="call study_get_topics for this course's topic ids",
                field_errors=[{"field": "topic_id", "problem": "no such topic"}],
            )
        if found.status != "proposed":
            raise StudyError(
                code="not_proposed",
                message=f"topic {topic_id!r} is {found.status}; only a proposed topic is decided",
                fix="nothing to do - it was already accepted or declined",
                field_errors=[{"field": "topic_id", "problem": f"status is {found.status}"}],
            )
        if decision == "accept":
            conn.execute(
                update(topic)
                .where(topic.c.id == topic_id, topic.c.user_id == user_id)
                .values(status="active", status_changed_at=now())
            )
            exam_ids = exam_assessments(conn, topic_id)
            for assessment_id in exam_ids:
                edge_if_missing(conn, user_id, topic_id, assessment_id)
            return {
                "topic_id": topic_id,
                "name": found.name,
                "status": "active",
                "examined_in": exam_ids,
            }
        assert retag is not None  # a decline without one was refused above
        return _decline(conn, user_id, found, retag)


def _retag_shape_errors(retag) -> list[dict]:
    """One field error per malformed entry: not an object, no text `item_id`, or a `topic` that is
    not exactly one of `{id}` / `{proposed_name}` with non-blank text."""
    if not isinstance(retag, list):
        return [{"field": "retag", "problem": "must be a list of {item_id, topic}"}]
    errors = []
    for i, entry in enumerate(retag):
        field = f"retag[{i}]"
        if not isinstance(entry, dict):
            errors.append({"field": field, "problem": "must be an object {item_id, topic}"})
            continue
        if not isinstance(entry.get("item_id"), str) or not entry["item_id"].strip():
            errors.append({"field": f"{field}.item_id", "problem": "must be an item id"})
        ref = entry.get("topic")
        if (
            not isinstance(ref, dict)
            or len(ref) != 1
            or not ({"id", "proposed_name"} & ref.keys())
            or not isinstance(next(iter(ref.values())), str)
            or not next(iter(ref.values())).strip()
        ):
            errors.append(
                {"field": f"{field}.topic", "problem": "must be {id: ...} or {proposed_name: ...}"}
            )
    return errors


def _decline(conn: Connection, user_id: str, found, retag: list[dict]) -> dict:
    """The decline half of `confirm_topic_proposal` (D-61 - D-63): check the map, resolve each
    target, move the items, retire the topic, then give each `active` target its edges."""
    topic_id = found.id
    declined = set(
        conn.execute(select(practice_item.c.id).where(practice_item.c.topic_id == topic_id))
        .scalars()
        .all()
    )
    items = [e["item_id"] for e in retag]
    missing = declined - set(items)
    foreign = set(items) - declined
    count = Counter(items)
    dups = [item_id for item_id, n in count.items() if n > 1]

    # every problem in one error, so the host fixes the whole list in one retry
    problems = []
    if missing:
        problems.append({"field": "retag", "problem": f"missing: {', '.join(sorted(missing))}"})
    if foreign:
        problems.append(
            {"field": "retag", "problem": f"not this topic's item: {', '.join(sorted(foreign))}"}
        )
    if dups:
        problems.append({"field": "retag", "problem": f"listed twice: {', '.join(sorted(dups))}"})
    if problems:
        raise StudyError(
            code="invalid_value",
            message=f"retag must name each of the {len(declined)} items of this topic exactly once",
            fix="call study_get_topics for this topic's item ids and send one entry per item",
            field_errors=problems,
        )

    # resolve targets: an {id} must be a live topic of this course; a name matches as in D-51.
    # A target equal to the declined topic (by id, or by its own name) would leave the item on it.
    targets = {}
    problems = []
    for i, entry in enumerate(retag):
        field = f"retag[{i}].topic"
        ref = entry["topic"]
        if "id" in ref and not is_live_topic(conn, user_id, found.course_id, ref["id"]):
            problems.append({"field": field, "problem": "not a live topic of this course"})
            continue
        target = topic_for(conn, user_id, found.course_id, ref)
        if target == topic_id:
            problems.append({"field": field, "problem": "is the topic being declined"})
            continue
        targets[entry["item_id"]] = target
    if problems:
        raise StudyError(
            code="invalid_value",
            message="some retag targets cannot take items",
            fix="point each item at another live topic of this course, by id or by a new name",
            field_errors=problems,
        )

    for item_id, target in targets.items():
        conn.execute(
            update(practice_item).where(practice_item.c.id == item_id).values(topic_id=target)
        )
    conn.execute(
        update(topic)
        .where(topic.c.id == topic_id)
        .values(status="declined", status_changed_at=now())
    )

    # D-60: a proposed target waits for its own accept; an active one gets its edges now
    rows = conn.execute(
        select(topic.c.id, topic.c.name, topic.c.status).where(
            topic.c.id.in_(set(targets.values()))
        )
    ).all()
    for row in rows:
        if row.status == "active":
            for assessment_id in exam_assessments(conn, row.id):
                edge_if_missing(conn, user_id, row.id, assessment_id)
    names = {row.id: row.name for row in rows}

    return {
        "topic_id": topic_id,
        "name": found.name,
        "status": "declined",
        "examined_in": [],
        "retagged": [
            {"item_id": item_id, "topic_id": target, "topic_name": names[target]}
            for item_id, target in targets.items()
        ],
    }
