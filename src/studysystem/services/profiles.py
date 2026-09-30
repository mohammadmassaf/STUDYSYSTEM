"""Exam profiles (1.13; D-14, D-21, D-67 - D-69): a slot's transcribed papers become the next
`exam_profile` version and its topic weights. Weights only in v1 - claim classes are M2.

`study_derive_profile` is one write unit (physical-schema.md, *Write units*). The weight math is
a separate pure function over a list of papers, so the M2 eval can hand it older papers only.
"""

from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import Engine, func, insert, select, update

from studysystem.db.tables import (
    exam_profile,
    generation_task,
    past_exam,
    practice_item,
    topic,
    topic_weight,
)
from studysystem.errors import StudyError
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_course, find_slot
from studysystem.services.units import write_unit

# Recency tiers (D-14): the newest paper, the two before it, every older one.
NEWEST, NEXT_TWO, OLDER = 3, 2, 1
MIN_PAPERS = 3  # below this a course runs cold-start (D-14, D-69)

# One paper as the math sees it: each item's (topic_id, marks), marks None when not printed.
PaperItems = Sequence[tuple[str, float | None]]


def topic_weights(papers: Sequence[PaperItems]) -> dict[str, float]:
    """Pooled topic weights for `papers`, given newest first. Returns {topic_id: weight}, the
    weights summing to 100. Pure: reads no database (D-67).

    An unmarked item takes the average printed mark of its topic across `papers`. A paper where
    every item then has a mark shares by marks; any other paper shares by item count. Each
    paper sums to 100, then papers pool by recency votes (NEWEST, NEXT_TWO, OLDER).

    I3302's three papers -> files 25.0, MySQL 23.2, sessions 17.2, forms 14.4, cookies 10.7,
    regex 9.5.
    """
    marks = defaultdict(list)
    for paper in papers:
        for topic_id, mark in paper:
            if mark is None:
                continue
            marks[topic_id].append(mark)
    average = {}
    for topic_id, values in marks.items():
        average[topic_id] = sum(values) / len(values)
    totals = defaultdict(float)  # each topic's share x vote, summed over the papers
    votes = 0
    for index, paper in enumerate(papers):
        if index == 0:
            vote = NEWEST
        elif index == 1 or index == 2:
            vote = NEXT_TWO
        else:
            vote = OLDER
        votes += vote

        filled = []  # this paper only: printed mark, else the topic's average, else None
        for topic_id, mark in paper:
            if mark is None:
                filled.append((topic_id, average.get(topic_id)))
            else:
                filled.append((topic_id, mark))
        has_mark = all(m is not None for _, m in filled)

        amounts = defaultdict(float)  # marks mode: each item adds its mark; count mode: 1
        for topic_id, mark in filled:
            amounts[topic_id] += mark if has_mark else 1
        paper_total = sum(amounts.values())
        for topic_id, amount in amounts.items():
            totals[topic_id] += amount / paper_total * 100 * vote

    return {topic_id: total / votes for topic_id, total in totals.items()}


def derive_profile(
    engine: Engine,
    user_id: str,
    code: str,
    assessment_name: str,
    semester_name: str | None = None,
) -> dict:
    """Derive the next `exam_profile` version for this course's slot `assessment_name`, as one
    write unit (D-69).

    Reads the slot's transcribed papers (newest first) and their items. Refuses
    `too_few_papers` below MIN_PAPERS, and `topics_unconfirmed` while an item sits on a
    `proposed` topic. Writes the profile row, one `topic_weight` row per topic, and moves open
    tasks built on an older profile of this slot to `invalidated`.

    Returns {"exam_profile_id", "version", "papers", "weights": [{"topic_id", "name",
    "weight"}], "invalidated"} - weights heaviest first, `invalidated` the number of tasks.
    """
    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        slot_id = find_slot(conn, user_id, course_id, assessment_name)

        past_exam_ids = (
            conn.execute(
                select(past_exam.c.id)
                .where(
                    past_exam.c.owner_id == user_id,
                    past_exam.c.slot_id == slot_id,
                    past_exam.c.transcript_task_id.is_not(None),
                )
                # Newest first; two papers on one day fall back to the later-registered id.
                .order_by(past_exam.c.session_date.desc(), past_exam.c.id.desc())
            )
            .scalars()
            .all()
        )
        if len(past_exam_ids) < MIN_PAPERS:
            raise StudyError(
                code="too_few_papers",
                message=f"{len(past_exam_ids)} transcribed papers; a profile needs {MIN_PAPERS}",
                fix="add and transcribe more past papers for this assessment (D-14)",
                field_errors=[{"field": "assessment", "problem": "too few transcribed papers"}],
            )
        items = []
        for p_id in past_exam_ids:
            rows = conn.execute(
                select(
                    practice_item.c.topic_id, practice_item.c.marks, topic.c.status, topic.c.name
                )
                .join_from(practice_item, topic, practice_item.c.topic_id == topic.c.id)
                .where(practice_item.c.past_exam_id == p_id)
                .order_by(practice_item.c.position)
            ).all()

            per_paper = []
            for row in rows:
                if row.status == "proposed":
                    raise StudyError(
                        code="topics_unconfirmed",
                        message=f"topic {row.name!r} ({row.topic_id}) is still proposed",
                        fix="decide it with study_confirm_topic_proposal, then derive again",
                        field_errors=[
                            {"field": "assessment", "problem": "a paper has a proposed topic"}
                        ],
                    )
                per_paper.append((row.topic_id, row.marks))
            items.append(per_paper)
        totals = topic_weights(items)
        version = conn.execute(
            select(func.max(exam_profile.c.version)).where(exam_profile.c.slot_id == slot_id)
        ).scalar_one_or_none()

        version = (version or 0) + 1

        profile_id = new_id()
        conn.execute(
            insert(exam_profile).values(
                id=profile_id,
                owner_id=user_id,
                slot_id=slot_id,
                version=version,
                derived_at=now(),
                provisional_syllabus=0,  # >= MIN_PAPERS, refused otherwise
                provisional_instructor=1,  # M2 derives it
                provisional_format=1,  # M2 derives it
                recency_weight_newest=NEWEST,
                recency_weight_next_two=NEXT_TWO,
                recency_weight_older=OLDER,
                evidence_count_syllabus=sum(len(paper) for paper in items),
                evidence_count_instructor=0,
                evidence_count_format=0,
            )
        )

        for topic_id, weight in totals.items():
            conn.execute(
                insert(topic_weight).values(
                    id=new_id(),
                    exam_profile_id=profile_id,
                    topic_id=topic_id,
                    weight=weight,
                    created_at=now(),
                )
            )

        # Tasks built on an older profile of this slot: their target weights just changed (D-11).
        older = select(exam_profile.c.id).where(
            exam_profile.c.slot_id == slot_id, exam_profile.c.id != profile_id
        )
        invalidated = conn.execute(
            update(generation_task)
            .where(
                generation_task.c.user_id == user_id,
                generation_task.c.status == "open",
                generation_task.c.exam_profile_id.in_(older),
            )
            .values(status="invalidated", closed_at=now())
        ).rowcount

        names = dict(
            conn.execute(select(topic.c.id, topic.c.name).where(topic.c.id.in_(list(totals))))
            .tuples()
            .all()
        )
    return {
        "exam_profile_id": profile_id,
        "version": version,
        "papers": len(past_exam_ids),
        "weights": [
            {"topic_id": topic_id, "name": names[topic_id], "weight": weight}
            for topic_id, weight in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "invalidated": invalidated,
    }
