"""Exam profiles (1.13; D-14, D-21, D-67 - D-69): a slot's transcribed papers become the next
`exam_profile` version, its topic weights, and the format and instructor-driven observations it
keeps - each class routed by its own rule (2.1b; D-156 - D-159), format by the session the
student sits next (2.3; D-168 - D-170).

`study_derive_profile` is one write unit (physical-schema.md, *Write units*). The weight math is
a separate pure function over a list of papers, so the M2 eval can hand it older papers only.
"""

import datetime
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import Engine, func, insert, select, update

from studysystem.db.tables import (
    assessment,
    course,
    exam_profile,
    generation_task,
    observation,
    past_exam,
    practice_item,
    profile_claim,
    topic,
    topic_weight,
)
from studysystem.errors import StudyError
from studysystem.services.claims import (
    assume_instructors,
    format_paper,
    format_provisional,
    instructor_provisional,
    route_claims,
)
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_course, find_slot
from studysystem.services.units import write_unit

# Recency tiers (D-14): the newest paper, the two before it, every older one.
NEWEST, NEXT_TWO, OLDER = 3, 2, 1
MIN_PAPERS = 3  # below this a course runs cold-start (D-14, D-69)

# One paper as the math sees it: each item's (topic_id, marks), marks None when not printed.
PaperItems = Sequence[tuple[str, float | None]]


def _zero_as_unmarked(paper: PaperItems) -> PaperItems:
    """`paper` with every mark None when its printed marks are all 0 - a host writing 0 for
    "not printed" (D-140). A paper with one real mark keeps its zeros."""
    printed = [mark for _, mark in paper if mark is not None]
    if printed and all(mark == 0 for mark in printed):
        return [(topic_id, None) for topic_id, _ in paper]
    return paper


def topic_weights(papers: Sequence[PaperItems]) -> dict[str, float]:
    """Pooled topic weights for `papers`, given newest first. Returns {topic_id: weight}, the
    weights summing to 100. Pure: reads no database (D-67).

    An unmarked item takes the average printed mark of its topic across `papers`. A paper where
    every item then has a mark, totalling more than 0, shares by marks; any other paper shares
    by item count. Each paper sums to 100, then papers pool by recency votes (NEWEST, NEXT_TWO,
    OLDER). A paper whose printed marks are all 0 is read as unmarked (D-140).

    I3302's three papers -> files 25.0, MySQL 23.2, sessions 17.2, forms 14.4, cookies 10.7,
    regex 9.5.
    """
    papers = [_zero_as_unmarked(paper) for paper in papers]
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
        has_mark = all(m is not None for _, m in filled) and sum(m for _, m in filled) > 0

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
    this_year: int | None = None,
) -> dict:
    """Derive the next `exam_profile` version for this course's slot `assessment_name`, as one
    write unit (D-69).

    Reads the slot's transcribed papers (newest first) and their `live` items; a `rejected`
    item is not evidence, and a paper with no live item is not in the set. Refuses
    `too_few_papers` below MIN_PAPERS, and `topics_unconfirmed` while a live item sits on a
    `proposed` topic. Writes the profile row, one `topic_weight` row per topic, one
    `profile_claim` row per format or instructor-driven observation of the same papers that
    `route_claims` keeps against `course.instructor` (D-156, D-159), a paper naming no one
    counted as its nearest named paper's (D-174) - format from the paper
    `format_paper` picks for the session of the slot's `assessment` row (D-168, D-169) - and
    moves open tasks built on an older profile of this slot to `invalidated`. `this_year` ages
    the papers for D-169's cap; None is the student's local year.

    Returns {"exam_profile_id", "version", "papers", "weights": [{"topic_id", "name",
    "weight"}], "claims": {"format", "instructor-driven"}, "provisional": {"syllabus",
    "instructor-driven", "format"}, "invalidated"} - weights heaviest first, `claims` the
    observations kept per class, `provisional` each class's flag as the row stores it with its
    reason (2.2; D-164 - D-167; assumed papers named in the instructor reason, D-175), `invalidated`
    the number of tasks.
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
                    past_exam.c.id.in_(
                        select(practice_item.c.past_exam_id).where(practice_item.c.state == "live")
                    ),
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
                message=(
                    f"{len(past_exam_ids)} transcribed papers with a live question; "
                    f"a profile needs {MIN_PAPERS}"
                ),
                fix="add and transcribe more past papers for this assessment (D-14, D-136)",
                field_errors=[
                    {"field": "assessment", "problem": "too few papers with a live question"}
                ],
            )
        items = []
        for p_id in past_exam_ids:
            rows = conn.execute(
                select(
                    practice_item.c.topic_id, practice_item.c.marks, topic.c.status, topic.c.name
                )
                .join_from(practice_item, topic, practice_item.c.topic_id == topic.c.id)
                .where(practice_item.c.past_exam_id == p_id, practice_item.c.state == "live")
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

        # The same papers' claims, routed by class (D-159). An instructor statement has no
        # paper, so none is read here - 2.4's override.
        course_instructor = conn.execute(
            select(course.c.instructor).where(course.c.id == course_id)
        ).scalar_one()
        paper_rows = {
            row.id: row
            for row in conn.execute(
                select(
                    past_exam.c.id,
                    past_exam.c.instructor,
                    past_exam.c.session_type,
                    past_exam.c.session_date,
                    past_exam.c.session_date_approx,
                ).where(past_exam.c.id.in_(past_exam_ids))
            )
        }
        # Each paper's date as the reasons print it: a month-only paper as YYYY-MM, not the 1st
        # it is stored as (D-137, D-181).
        shown = {
            p_id: row.session_date[:7] if row.session_date_approx else row.session_date
            for p_id, row in paper_rows.items()
        }
        # Who each paper counts as: a paper naming no one takes its nearest named one's (D-174).
        counted_as = assume_instructors(
            [paper_rows[p_id].instructor for p_id in past_exam_ids], course_instructor
        )
        instructor_of = [  # (name, was assumed, date) per paper, for the instructor reason (D-175)
            (name, was_assumed, shown[p_id])
            for (name, was_assumed), p_id in zip(counted_as, past_exam_ids, strict=True)
        ]
        # The session he sits next: the slot's sitting, `first` when it has a resit too (D-168).
        session = conn.execute(
            select(assessment.c.session_type)
            .where(assessment.c.user_id == user_id, assessment.c.slot_id == slot_id)
            .order_by(assessment.c.session_type)
            .limit(1)
        ).scalar_one_or_none()
        claims_of = defaultdict(list)  # paper id -> [(observation id, class)]
        class_of = {}
        for obs in conn.execute(
            select(observation.c.id, observation.c.past_exam_id, observation.c.claim_class)
            .where(
                observation.c.owner_id == user_id,
                observation.c.slot_id == slot_id,
                observation.c.past_exam_id.in_(past_exam_ids),
                observation.c.claim_class.in_(["format", "instructor-driven"]),
            )
            .order_by(observation.c.id)
        ):
            claims_of[obs.past_exam_id].append((obs.id, obs.claim_class))
            class_of[obs.id] = obs.claim_class
        papers = [
            (name, claims_of[p_id])
            for (name, _), p_id in zip(counted_as, past_exam_ids, strict=True)
        ]
        format_papers = [
            (
                paper_rows[p_id].session_type,
                shown[p_id],
                any(claim_class == "format" for _, claim_class in claims_of[p_id]),
            )
            for p_id in past_exam_ids
        ]
        if this_year is None:
            this_year = datetime.datetime.now().astimezone().year  # his local year
        format_at = format_paper(format_papers, session, this_year)
        kept = route_claims(papers, course_instructor, format_at)
        kept_per_class = {"format": 0, "instructor-driven": 0}
        for obs_id in kept:
            kept_per_class[class_of[obs_id]] += 1

        # Per class, as the row stores it and the reply states it (D-164, D-165, D-167, D-170).
        provisional_by_class = {
            "syllabus": {
                "provisional": False,  # below MIN_PAPERS derive refuses (D-164)
                "why": f"{len(past_exam_ids)} papers; {MIN_PAPERS} needed",
            },
            "instructor-driven": instructor_provisional(instructor_of, course_instructor),  # D-175
            "format": format_provisional(format_papers, session, this_year, format_at),  # D-170
        }

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
                provisional_syllabus=int(provisional_by_class["syllabus"]["provisional"]),
                provisional_instructor=int(
                    provisional_by_class["instructor-driven"]["provisional"]
                ),
                provisional_format=int(provisional_by_class["format"]["provisional"]),
                recency_weight_newest=NEWEST,
                recency_weight_next_two=NEXT_TWO,
                recency_weight_older=OLDER,
                evidence_count_syllabus=sum(len(paper) for paper in items),
                evidence_count_instructor=kept_per_class["instructor-driven"],  # D-158
                evidence_count_format=kept_per_class["format"],
                assumed_instructor_papers=sum(was for _, was, _ in instructor_of),  # D-176
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

        for obs_id in kept:
            conn.execute(
                insert(profile_claim).values(
                    id=new_id(),
                    exam_profile_id=profile_id,
                    observation_id=obs_id,
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
        "claims": kept_per_class,
        "provisional": provisional_by_class,
        "invalidated": invalidated,
    }
