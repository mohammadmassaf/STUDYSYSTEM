"""Split and merge (2.11; D-13, D-17 9a, D-22, D-192 - D-197, D-200, D-201, D-203, D-204): the
only way to fix a topic once it is `active`. Both retire the old topic by status and move what hangs
off it - its items, their attempts, edges added onto the new topic. Neither deletes a row,
moves a coverage edge or touches a profile: the result names the slots to derive again (D-194).

`study_split_topic` and `study_merge_topics` are one write unit each (physical-schema.md,
*Write units*). `lineage_violations` is the *Lineage* invariant, read back by its test.
"""

from sqlalchemy import Connection, Engine, func, insert, select, update

from studysystem.db.tables import (
    assessment_slot,
    attempt,
    course,
    coverage,
    exam_profile,
    material,
    practice_item,
    topic,
    topic_split,
    topic_state,
    topic_weight,
)
from studysystem.errors import StudyError
from studysystem.services.ids import new_id, now
from studysystem.services.study_loop import PRACTISABLE, store_state
from studysystem.services.topics import (
    TAGGABLE,
    edge_if_missing,
    exam_assessments,
    map_problems,
    material_edge_if_missing,
)
from studysystem.services.units import write_unit

# --- the two shared pieces ----------------------------------------------------------------


def move_items(
    conn: Connection, user_id: str, from_id: str, to_id: str, item_ids: list[str] | None
) -> None:
    """Move practice items from one topic to another, with everything that must follow them
    (D-192, D-195, D-197). The caller has already checked both topics and the ids.

    item_ids: the items to move - a split sends one child's share of the parent's live items;
    None moves every item of `from_id`, in any state (a merge).

    After it returns:
    - each moved item's `topic_id` is `to_id`
    - each attempt on a moved item has `topic_id` = `to_id` - the *Same course* invariant
      (`attempt.topic_id` = its item's `topic_id`)
    - with None only, also the attempts of `from_id` that name no item (self-reported,
      exam-retro): a merge leaves nothing on the source (D-195)
    - `to_id` has an exam edge (if missing) per assessment `exam_assessments` returns for it
    - `to_id` has a material edge (if missing) per material `from_id` has an edge to (D-197)
    - no coverage row has moved or gone; `from_id` keeps its own edges

    Returns nothing.

    Examples, I3302:
    - merge sessions -> cookies, None: sessions' 7 items and their attempts move. Cookies already
      has its Final and Partial edges, so no edge is written - the collision D-192 is about never
      happens, because nothing is moved onto it.
    - split PHP + MySQL -> "MySQL queries", 9 ids: those 9 and their attempts move; the new topic
      gets its Final edge. The other 16 items wait for the other child's call.
    """
    if item_ids is None:
        which_items = practice_item.c.topic_id == from_id
        which_attempts = attempt.c.topic_id == from_id
    else:
        which_items = practice_item.c.id.in_(item_ids)
        which_attempts = attempt.c.practice_item_id.in_(item_ids)
    conn.execute(update(practice_item).where(which_items).values(topic_id=to_id))
    conn.execute(update(attempt).where(which_attempts).values(topic_id=to_id))
    # 3a: after the move, so `exam_assessments` reads the items `to_id` now holds
    for assessment_id in exam_assessments(conn, to_id):
        edge_if_missing(conn, user_id, to_id, assessment_id)
    # 3b: the old topic's chapters, copied - its own edges stay where they are (D-197)
    materials = conn.execute(
        select(coverage.c.material_id).where(
            coverage.c.topic_id == from_id, coverage.c.material_id.is_not(None)
        )
    ).scalars()
    for material_id in materials.all():
        material_edge_if_missing(conn, user_id, to_id, material_id)


def lineage_violations(conn: Connection, user_id: str) -> list[str]:
    """The *Lineage* invariant (physical-schema.md, D-22): the ids of this user's `superseded`
    topics whose lineage is wrong, sorted. Empty means every retired topic is healthy.

    A `superseded` topic is healthy in exactly one of two shapes:
    - merge: `superseded_by` set, and no `topic_split` row names it as parent
    - split: `superseded_by` NULL, and two or more `topic_split` rows name it as parent

    Wrong: both (a merge link and children), neither (no link, no children), or a split with one
    child. A topic in any other status is not this check's business.

    Examples, I3302 after the demo:
    - PHP + MySQL: no link, 2 children -> healthy
    - PHP sessions: superseded_by cookies, 0 children -> healthy
    - a topic set to `superseded` by hand, no link, no children -> its id is returned

    Portable SQL (D-08): it runs on SQLite and Postgres.
    """
    # per topic, the topic_split rows naming it as parent - re-counted for each outer row
    children = (
        select(func.count(topic_split.c.id))
        .where(topic_split.c.parent_topic_id == topic.c.id)
        .scalar_subquery()
    )
    rows = conn.execute(
        select(topic.c.id, topic.c.superseded_by, children.label("children"))
        .where(topic.c.user_id == user_id, topic.c.status == "superseded")
        .order_by(topic.c.id)
    ).all()
    violations = []
    for row in rows:
        if row.superseded_by is not None:
            healthy = row.children == 0  # a merge has no children
        else:
            healthy = row.children >= 2  # a split has two or more
        if not healthy:
            violations.append(row.id)
    return violations


# --- helpers ------------------------------------------------------------------------------


def _fixable_topic(conn: Connection, user_id: str, topic_id: str, field: str):
    """The topic row, when it is this user's and `active` / `unexamined` - the statuses that
    take attempts (PRACTISABLE): a `proposed` topic has decline for this (D-61), a `superseded`
    or `declined` one is already retired. Else a refusal naming `field`."""
    found = conn.execute(
        select(topic).where(topic.c.id == topic_id, topic.c.user_id == user_id)
    ).one_or_none()
    if found is None:
        raise StudyError(
            code="not_found",
            message=f"no topic {topic_id!r}",
            fix="call study_get_topics for this course's topic ids",
            field_errors=[{"field": field, "problem": "no such topic"}],
        )
    if found.status not in PRACTISABLE:
        fix = {
            "proposed": "a proposed topic is fixed with study_confirm_topic_proposal (decline)",
        }.get(found.status, "it is already retired - use the topic it became")
        raise StudyError(
            code="not_fixable",
            message=f"topic {topic_id!r} is {found.status}; only an active or unexamined topic "
            "is split or merged",
            fix=fix,
            field_errors=[{"field": field, "problem": f"status is {found.status}"}],
        )
    return found


def _examined_if_asked(conn: Connection, topic_id: str) -> None:
    """An `unexamined` topic that now holds a live past-exam item becomes `active` (D-204): a
    topic a paper asks about is examined. Any other status is left alone."""
    if exam_assessments(conn, topic_id):
        conn.execute(
            update(topic)
            .where(topic.c.id == topic_id, topic.c.status == "unexamined")
            .values(status="active", status_changed_at=now())
        )


def _materials(conn: Connection, topic_id: str) -> list[dict]:
    """The materials the topic has an edge to, by filename - what D-197 copies and lists."""
    rows = conn.execute(
        select(material.c.id, material.c.filename)
        .join_from(coverage, material, material.c.id == coverage.c.material_id)
        .where(coverage.c.topic_id == topic_id)
        .order_by(material.c.filename)
    ).all()
    return [{"material_id": row.id, "filename": row.filename} for row in rows]


def _derive_again(conn: Connection, topic_id: str) -> list[dict]:
    """Each slot whose **latest** profile weights the retired topic, with the call that fixes it
    (D-194). Empty when no latest profile names it.

    I3302 after merging sessions into cookies -> [{"slot": "Final exam", "fix": "call
    study_derive_profile with code 'I3302' and assessment_name 'Final exam'"}]."""
    newer = exam_profile.alias("newer")
    latest = (
        select(func.max(newer.c.version))
        .where(newer.c.slot_id == exam_profile.c.slot_id)
        .scalar_subquery()
    )
    rows = conn.execute(
        select(assessment_slot.c.name, course.c.code)
        .join_from(topic_weight, exam_profile, exam_profile.c.id == topic_weight.c.exam_profile_id)
        .join(assessment_slot, assessment_slot.c.id == exam_profile.c.slot_id)
        .join(course, course.c.id == assessment_slot.c.course_id)
        .where(topic_weight.c.topic_id == topic_id, exam_profile.c.version == latest)
        .order_by(assessment_slot.c.name)
    ).all()
    return [
        {
            "slot": row.name,
            "fix": f"call study_derive_profile with code {row.code!r} and assessment_name "
            f"{row.name!r}",
        }
        for row in rows
    ]


def _children_shape_errors(children) -> list[dict]:
    """One field error per malformed part of a split's `children`, checked before the database
    opens: a list of 2+ `{name, item_ids}`, each name non-blank and distinct (ignoring case),
    each `item_ids` a non-empty list of ids."""
    if not isinstance(children, list):
        return [{"field": "children", "problem": "must be a list of {name, item_ids}"}]
    errors = []
    if len(children) < 2:
        errors.append({"field": "children", "problem": "a split needs 2 or more children"})
    seen = {}
    for i, child in enumerate(children):
        field = f"children[{i}]"
        if not isinstance(child, dict):
            errors.append({"field": field, "problem": "must be an object {name, item_ids}"})
            continue
        name = child.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append({"field": f"{field}.name", "problem": "must be a non-blank name"})
        else:
            key = name.strip().lower()
            if key in seen:
                errors.append(
                    {"field": f"{field}.name", "problem": f"same name as children[{seen[key]}]"}
                )
            seen.setdefault(key, i)
        ids = child.get("item_ids")
        if (
            not isinstance(ids, list)
            or not ids
            or not all(isinstance(x, str) and x.strip() for x in ids)
        ):
            errors.append(
                {"field": f"{field}.item_ids", "problem": "must be a non-empty list of item ids"}
            )
    return errors


# --- the two tools ------------------------------------------------------------------------


def split_topic(engine: Engine, user_id: str, topic_id: str, children) -> dict:
    """Split one `active` / `unexamined` topic into 2+ new ones, as one write unit (D-13).

    `children` is `[{name, item_ids}]`: each child's name and its share of the parent's live
    items - every live item named exactly once (D-195, decline's rule), each child at least one,
    no name a live topic's (D-201). A rejected item and an item-less attempt stay on the parent.

    Writes: each child (the parent's status, `proposed_by = 'split'`, D-201) and its
    `topic_split` row; the items
    and their attempts, moved; the parent's `topic_state` copied to each child with
    `inferred = 1`, none when the parent has none (D-193); the edges (D-192, D-197); the parent ->
    `superseded`, `superseded_by` NULL.

    Returns {"topic_id", "name", "status", "children": [{"topic_id", "name", "item_ids"}],
    "retag_materials", "derive_again"}.
    """
    shape = _children_shape_errors(children)
    if shape:
        raise StudyError(
            code="invalid_value",
            message="children must be 2 or more {name, item_ids}, with distinct names",
            fix="send children as [{name, item_ids}, ...] - study_get_topics with "
            "include_hidden true lists every item id",
            field_errors=shape,
        )

    with write_unit(engine) as conn:
        parent = _fixable_topic(conn, user_id, topic_id, "topic_id")

        # the map: every live item of the parent exactly once (D-195, a decline's check)
        live = set(
            conn.execute(
                select(practice_item.c.id).where(
                    practice_item.c.topic_id == topic_id, practice_item.c.state == "live"
                )
            )
            .scalars()
            .all()
        )
        listed = [item_id for child in children for item_id in child["item_ids"]]
        problems = map_problems(live, listed, "children")

        # names: none may be a live topic's of this course - the parent's included; compared as
        # the partial unique index compares them (D-201)
        for i, child in enumerate(children):
            taken = conn.execute(
                select(topic.c.name, topic.c.status).where(
                    topic.c.user_id == user_id,
                    topic.c.course_id == parent.course_id,
                    func.lower(topic.c.name) == func.lower(child["name"].strip()),
                    topic.c.status.in_(TAGGABLE),
                )
            ).one_or_none()
            if taken is None:
                continue
            if taken.status == "proposed":
                problem = (
                    f"proposed topic {taken.name!r} has it - decide that proposal with "
                    "study_confirm_topic_proposal first, or pick another name"
                )
            else:
                problem = (
                    f"live topic {taken.name!r} has it - to fold items into it, use "
                    "study_merge_topics; or pick another name"
                )
            problems.append({"field": f"children[{i}].name", "problem": problem})
        if problems:
            raise StudyError(
                code="invalid_value",
                message=f"children must name each of the {len(live)} live items of this topic "
                "exactly once, under new names",
                fix="call study_get_topics with include_hidden true for every item id of this "
                "topic, and fix each problem named",
                field_errors=problems,
            )

        state = conn.execute(
            select(topic_state).where(
                topic_state.c.user_id == user_id, topic_state.c.topic_id == topic_id
            )
        ).one_or_none()
        materials = _materials(conn, topic_id)
        stamp = now()
        made = []
        for child in children:
            child_id = new_id()
            name = child["name"].strip()
            conn.execute(
                insert(topic).values(
                    id=child_id,
                    user_id=user_id,
                    course_id=parent.course_id,
                    name=name,
                    status=parent.status,  # D-201; active once it holds a paper's item (D-204)
                    proposed_by="split",
                    status_changed_at=stamp,
                    created_at=stamp,
                )
            )
            conn.execute(
                insert(topic_split).values(
                    id=new_id(),
                    user_id=user_id,
                    parent_topic_id=topic_id,
                    child_topic_id=child_id,
                    created_at=stamp,
                )
            )
            move_items(conn, user_id, topic_id, child_id, child["item_ids"])
            _examined_if_asked(conn, child_id)
            if state is not None:  # the parent's numbers, marked as an estimate (D-193)
                copy = {
                    **state._mapping,
                    "topic_id": child_id,
                    "inferred": True,
                    "updated_at": stamp,
                }
                conn.execute(insert(topic_state).values(copy))
            made.append({"topic_id": child_id, "name": name, "item_ids": child["item_ids"]})

        conn.execute(
            update(topic)
            .where(topic.c.id == topic_id)
            .values(status="superseded", status_changed_at=stamp)
        )
        return {
            "topic_id": topic_id,
            "name": parent.name,
            "status": "superseded",
            "children": made,
            "retag_materials": materials,
            "derive_again": _derive_again(conn, topic_id),
        }


def merge_topics(engine: Engine, user_id: str, source_id: str, target_id: str) -> dict:
    """Fold one `active` / `unexamined` topic into another of the same course, as one write
    unit (D-13).

    Writes: every item of the source in any state, every attempt, moved to the target (D-195);
    the edges (D-192, D-197); the source -> `superseded`, `superseded_by` = target; then, when a
    moved attempt counts, the target's `topic_state` replayed over all its attempts - the
    combined state - else left as it is, a split child's `inferred` copy included (D-200). The
    source is replayed too, which deletes its row: no attempt is left on it (D-107).

    Returns {"topic_id", "name", "status", "superseded_by", "target": {"topic_id", "name",
    "state"}, "retag_materials", "derive_again"}.
    """
    if source_id == target_id:
        raise StudyError(
            code="invalid_value",
            message="a topic cannot merge into itself",
            fix="send the topic to retire as source_id and the one that takes its items as "
            "target_id",
            field_errors=[{"field": "target_id", "problem": "same as source_id"}],
        )

    with write_unit(engine) as conn:
        source = _fixable_topic(conn, user_id, source_id, "source_id")
        target = _fixable_topic(conn, user_id, target_id, "target_id")
        if target.course_id != source.course_id:
            raise StudyError(
                code="invalid_value",
                message="the two topics are in different courses",
                fix="merge only topics of one course - study_get_topics lists them",
                field_errors=[{"field": "target_id", "problem": "another course's topic"}],
            )

        materials = _materials(conn, source_id)
        # new evidence for the target: an attempt the replay reads (D-107's filter, D-200)
        counted = conn.execute(
            select(func.count(attempt.c.id)).where(
                attempt.c.topic_id == source_id,
                attempt.c.assisted == 0,  # INTEGER 0/1: IS FALSE fails on Postgres
                attempt.c.correctness_voided == 0,
            )
        ).scalar_one()
        move_items(conn, user_id, source_id, target_id, None)
        _examined_if_asked(conn, target_id)
        conn.execute(
            update(topic)
            .where(topic.c.id == source_id)
            .values(status="superseded", superseded_by=target_id, status_changed_at=now())
        )
        if counted:  # else the target's state stays as it is, a split child's copy included
            store_state(conn, user_id, target_id, None)
        store_state(conn, user_id, source_id, None)  # no attempt left: its row goes
        state = conn.execute(
            select(topic_state.c.due_at, topic_state.c.reps, topic_state.c.lapses).where(
                topic_state.c.user_id == user_id, topic_state.c.topic_id == target_id
            )
        ).one_or_none()
        state = None if state is None else dict(state._mapping)
        return {
            "topic_id": source_id,
            "name": source.name,
            "status": "superseded",
            "superseded_by": target_id,
            "target": {"topic_id": target_id, "name": target.name, "state": state},
            "retag_materials": materials,
            "derive_again": _derive_again(conn, source_id),
        }
