"""Scheduler v1 (1.15; D-12, D-15, D-22, D-82 - D-101): `weight x weakness x proximity / hours`
over every course, in three lanes - review, study, setup.

`study_get_plan` is one write unit (physical-schema.md, *Write units*): one `plan` row and every
`plan_item`, the full ranking, with the top `shown_n` of each lane marked `shown`. SQL gathers
the rows; pure functions score them and write the reason (D-88), so the math is tested without a
database.
"""

import datetime
import json
import logging
import time
from collections import Counter
from collections.abc import Iterable, Sequence

from sqlalchemy import (
    CompoundSelect,
    Connection,
    Engine,
    Select,
    and_,
    exists,
    func,
    insert,
    or_,
    select,
    true,
    union,
)

from studysystem.db.tables import (
    assessment,
    assessment_slot,
    course,
    coverage,
    exam_profile,
    hours_entry,
    material,
    note,
    past_exam,
    plan,
    plan_item,
    study_session,
    topic,
    topic_state,
    topic_weight,
)
from studysystem.services.ids import new_id, now
from studysystem.services.profiles import MIN_PAPERS
from studysystem.services.study_loop import retrievability
from studysystem.services.units import write_unit
from studysystem.services.values import invalid

log = logging.getLogger(__name__)

SHOWN_N = 5  # items per lane the host is sent and that are marked shown (D-87, D-92)
COLD_WEAKNESS = 1.0  # a topic with no attempts (D-82)
ESTIMATED_MINUTES = 60  # flat cost of one topic, inferred (D-83)
SNOOZE_DAYS = 7  # hours logged against a setup key hide it this long (D-22)
UNKNOWN_EXAM_WEIGHT = 30.0  # an exam whose weight is unknown scores as a Partial, inferred (D-96)
PAPERS_WANTED = 5  # a slot asks for past papers until this many land (D-22)


def study_score(exams: Sequence[tuple[float, int]], weakness: float, minutes: int) -> float:
    """One study item's score (D-84, D-86, D-89). Pure: reads no database.

    `exams` holds one (marks, days) pair per upcoming exam the topic reaches: marks already
    x credits, days already clamped to >= 1. Score = sum of marks / days, x weakness, / hours.

    PHP + MySQL today: [(110.0, 106)], 1.0, 60 -> 1.038  (70 x 39.3% = 27.5 marks x 4 credits)
    The same topic also on the Partial at 20 marks x 4: [(110.0, 106), (80.0, 43)] -> 2.898

    Raises ValueError on an empty `exams`: a topic with no upcoming exam is filtered before it
    is scored (D-94), so an empty list here is a caller bug, never a score of 0.
    """
    if not exams:
        raise ValueError("a topic with no upcoming exam has no score; filter it first (D-94)")
    proximity_marks = sum(marks / days for marks, days in exams)
    return proximity_marks * weakness / (minutes / 60)


SHARE_NOTES = {"average": "average share, inferred", "equal split": "equal split, no profile"}
DATE_NOTES = {
    "exact": "",
    "approximate": "approximate date",
    "borrowed": "no date - the latest exam date borrowed",
    "unknown": "no exam has a date yet",
}


def study_reason(entry: dict, weakness: float, minutes: int) -> str:
    """The stored `reason` for one study item: `entry` from `_topic_exams`. Pure. It names every
    input that is `inferred` or `unknown`, so the line still explains the pick months later
    without recomputing it (ticket 13, D-12).

    PHP + MySQL today -> "I3302 PHP + MySQL (prepared statements): Final exam: 27.5 of 100 marks
    (profile v2), in 106 days (approximate date) · x 4 credits · no attempts yet (weakness 1,
    inferred) · 60 min estimate (inferred)"
    """
    parts = []
    for exam in entry["exams"]:
        marks = exam["weight"] * exam["share"] / 100
        share = SHARE_NOTES.get(exam["share_source"], f"profile v{exam['profile_version']}")
        if exam["weight_tier"] != "declared":
            share += f"; exam weight {exam['weight']:g}, {exam['weight_tier']}"
        when = f"in {exam['days']} days"
        note = DATE_NOTES[exam["date_source"]]
        if exam["date_source"] == "approximate" and exam["date_reached"]:
            note = "approximate date reached - enter the exact date"  # D-95
        if note:
            when += f" ({note})"
        parts.append(f"{exam['exam_name']}: {marks:.1f} of 100 marks ({share}), {when}")

    credits = f"x {entry['credits']:g} credits"
    if entry["credits_tier"] == "unknown":
        credits += " (no course declares credits yet - 1 used, D-98)"
    elif entry["credits_tier"] != "declared":
        credits += " (average of declared courses, inferred)"
    if entry["stability"] is None:
        state = f"no attempts yet (weakness {weakness:g}, inferred)"
    else:
        state = f"weakness {weakness:.2f} (FSRS: {1 - weakness:.0%} recall today)"
    what = f"{entry['code']} {entry['topic_name']}"  # named like a setup reason: course first
    exams = " + ".join(parts)
    return f"{what}: " + " · ".join([exams, credits, state, f"{minutes} min estimate (inferred)"])


SETUP_ASKS = {
    "grading": "confirm the grading breakdown - some exam weights are still guesses",
    "credits": "declare the course credits - every mark of the course is weighted by them",
    "exam-date": "enter the exam date - its topics rank on a guessed one",
    "target-grade": "set a target grade - the shortfall check needs it",
    "first-material": "feed a chapter and get its notes - nothing taught yet",
    "past-papers": "get more past papers",
}


def setup_reason(item: dict) -> str:
    """The stored `reason` for one setup item from `_setup_items`. Pure.

    I3350 today -> "I3350: feed a chapter and get its notes - nothing taught yet · could move 500
    (marks x credits)"
    """
    where = item["code"] if item["target_name"] is None else f"{item['code']} {item['target_name']}"
    ask = SETUP_ASKS[item["setup_kind"]]
    estimate = item["marks_unlocked_estimate"]
    if item["setup_kind"] == "past-papers" and estimate == 0:
        ask += " - the profile has its papers; papers 4-5 unlock only the mock-exam gate (D-24)"
    worth = f"could move {estimate:g} (marks x credits)" if estimate else "moves no marks itself"
    return f"{where}: {ask} · {worth}"


def review_reason(row) -> str:
    """The stored `reason` for one due review from `_due_reviews`. Pure.

    -> "I3302: PHP sessions - review due since 2026-10-01"
    """
    return f"{row.code}: {row.topic_name} - review due since {row.due_at[:10]}"


def _average_credits(credits: Iterable[float | None]) -> float | None:
    """D-84's fallback for unknown credits: the average over the courses that declare theirs.
    None when no course declares any - the caller then uses 1, which reorders nothing (D-98).
    Pure."""
    declared = [value for value in credits if value is not None]
    return sum(declared) / len(declared) if declared else None


def _topic_exams(
    rows: Sequence,
    today: datetime.date,
    average_credits: float | None,
    latest_exam_date: datetime.date | None,
) -> dict:
    """Group `_study_rows` by topic: {topic_id: the topic's course, credits, stability, and its
    exams - each with marks, days and where each number came from}. Pure: reads no database.

    Share: the profile's; a profile without this topic -> 100 / profile_size ("average"); no
    profile -> 100 / topics reaching that exam ("equal split") (D-89, D-90). Marks = exam weight
    x share / 100 x credits; NULL credits -> `average_credits`, inferred (D-84). Days = date -
    today; NULL date -> `latest_exam_date`, borrowed (D-86); below 1 -> 1 (D-95).

    MySQL today -> one exam: 70 x 39.34% x 4 = 110.2 marks, 106 days, share from profile v2.
    PHP sessions on the Final and a Partial with no profile, 3 topics on it -> Final 46.7 marks,
    106 days; Partial 30 x 33.3% x 4 = 40.0 marks, 43 days, equal split.

    Each topic's `exam_profile_id` is the profile of its exam with the largest marks / days
    term (D-97). A fallback with nothing to fall back on - no course declares credits, no exam
    anywhere has a date - uses one shared value (credits 1, days 1): every course or exam is in
    the same case then, so it reorders nothing.
    """
    # Pass 1: how many topics reach each exam - the equal split's n (D-89, D-90). Distinct
    # pairs: `_study_rows`' UNION already returns a pair once, whichever path reached it (an
    # exam edge, a chapter, or both); this keeps a repeated pair from counting a topic twice.
    pairs = {(row.assessment_id, row.topic_id) for row in rows}
    reaching = Counter(assessment_id for assessment_id, _ in pairs)

    # Pass 2: one exam entry per row, collected under its topic - the merge.
    topics = {}
    seen = set()
    for row in rows:
        if (row.assessment_id, row.topic_id) in seen:
            continue  # the same pair twice must not score the exam twice
        seen.add((row.assessment_id, row.topic_id))

        if row.credits is not None:
            credits, credits_tier = row.credits, row.credits_tier
        elif average_credits is not None:
            credits, credits_tier = average_credits, "inferred"
        else:
            credits, credits_tier = 1.0, "unknown"  # no course declares credits (D-98)

        entry = topics.setdefault(
            row.topic_id,
            {
                "topic_id": row.topic_id,
                "topic_name": row.topic_name,
                "course_id": row.course_id,
                "code": row.code,
                "credits": credits,
                "credits_tier": credits_tier,
                "stability": row.stability,
                "last_reviewed_at": row.last_reviewed_at,
                "exams": [],
            },
        )

        if row.exam_weight is None:
            weight, weight_tier = UNKNOWN_EXAM_WEIGHT, "inferred"
        else:
            weight, weight_tier = row.exam_weight, row.exam_weight_tier

        if row.exam_profile_id is None:
            share, share_source = 100 / reaching[row.assessment_id], "equal split"
        elif row.share is None:
            share, share_source = 100 / row.profile_size, "average"
        else:
            share, share_source = row.share, "profile"

        if row.date is not None:
            days = (datetime.date.fromisoformat(row.date) - today).days
            date_source = "approximate" if row.date_approx else "exact"
        elif latest_exam_date is not None:
            days = (latest_exam_date - today).days
            date_source = "borrowed"
        else:
            days, date_source = 1, "unknown"

        entry["exams"].append(
            {
                "assessment_id": row.assessment_id,
                "exam_name": row.exam_name,
                "weight": weight,
                "weight_tier": weight_tier,
                "share": share,
                "share_source": share_source,
                "exam_profile_id": row.exam_profile_id,
                "profile_version": row.profile_version,
                "marks": weight * share / 100 * credits,
                "days": max(days, 1),  # an exam today, or an approximate date passed (D-95)
                "date_reached": days < 1,  # before the clamp: today or already passed
                "date_source": date_source,
            }
        )

    for entry in topics.values():
        top = max(entry["exams"], key=lambda exam: exam["marks"] / exam["days"])
        entry["exam_profile_id"] = top["exam_profile_id"]
    return topics


def _taught_materials(user_id: str) -> CompoundSelect:
    """The ids of the user's taught materials (D-120), as a subquery to put inside IN: a chapter
    is taught once it has a note, or a study session whose subject is that chapter. Hours logged
    on a topic do not count - one topic on two chapters would mark both taught.

    I3304 on 2026-10-09 -> chapter 1 (its notes landed at 08:24, four minutes after the feed).
    """
    noted = select(note.c.material_id).where(note.c.user_id == user_id)
    studied = select(study_session.c.subject_id).where(
        study_session.c.user_id == user_id, study_session.c.subject_type == "material"
    )
    return union(noted, studied)


def _study_rows(
    conn: Connection, user_id: str, today: datetime.date, taught_only: bool = True
) -> list:
    """The hand-written SQL behind the study lane (D-08, D-88): every `active` topic of the user
    with each upcoming exam it reaches - the assessment's weight, date and status, the course's
    credits, the topic's share in that slot's **latest** profile (NULL when it has none) - and
    its `topic_state`, if any.

    A topic reaches an exam two ways, one SELECT each, joined by a UNION:
    - **by an exam edge** `topic -> assessment` (D-60), whatever the exam's profile;
    - **by a chapter** (D-90, D-102): the topic has an edge to a material of its course, and the
      exam is one of that course's upcoming exams with **no profile** - "every chapter received
      so far counts toward it" until its chapter list is declared (no tool declares one yet).
    A pair reached both ways is one row: UNION drops identical rows.

    **Taught so far** (D-118 - D-120): a row is kept only if its topic has an edge to a taught
    chapter (`_taught_materials`), or its exam is in prep - `prep_from` is today or earlier. In
    prep an untaught topic comes back on that exam only; an exam still in term mode adds nothing.
    `taught_only=False` drops this filter - only for the log's `untaught` count.

    I3304 on 2026-10-09, chapter 1 noted and tagged, no exam in prep -> 4 rows: frame hex
    decoding on both profiled exams, IP addressing and protocol layers on the Final (their only
    exam edges); its 15 other topics none.

    I3302 on 2026-10-05, Chapter 6 tagged to MySQL only, both exams in prep -> 8 rows: 7 by exam
    edge, one per topic, each to the Final (2027-01-18, approx) through profile v2 (MySQL 39.3,
    sessions 16.7, files 13.5, cookies 12.7, strings 6.5, forms 6.2, regex 5.1); 1 by chapter,
    MySQL to the Partial (2026-11-16), which has no profile. No `topic_state` on any.

    An exam counts while its slot is an exam, it is `upcoming`, and its date is NULL, today or
    later, or approximate - only an exact date that has passed drops it (D-95).
    """
    # A second copy of exam_profile, so the subquery reads its own rows instead of correlating
    # with the outer query's exam_profile (auto-correlation).
    newer = exam_profile.alias("newer")
    latest_version = (
        select(func.max(newer.c.version))
        .where(newer.c.slot_id == assessment_slot.c.id)
        .scalar_subquery()
    )
    # How many topics the row's profile weights: D-89's average share is 100 / this. Counted
    # over the profile, not over these rows - a topic missing from the profile is in the rows.
    # No profile -> 0 (a count over nothing), so "no profile" is read from exam_profile_id.
    weighted = topic_weight.alias("weighted")
    profile_size = (
        select(func.count(weighted.c.id))
        .where(weighted.c.exam_profile_id == exam_profile.c.id)
        .scalar_subquery()
    )
    # What both SELECTs return, in the same order - a UNION lines its columns up by place.
    columns = select(
        topic.c.id.label("topic_id"),
        topic.c.name.label("topic_name"),
        course.c.id.label("course_id"),
        course.c.code,
        course.c.credits,
        course.c.credits_tier,
        assessment.c.id.label("assessment_id"),
        assessment_slot.c.name.label("exam_name"),
        assessment.c.weight.label("exam_weight"),
        assessment.c.weight_tier.label("exam_weight_tier"),
        assessment.c.date,
        assessment.c.date_approx,
        exam_profile.c.id.label("exam_profile_id"),
        exam_profile.c.version.label("profile_version"),
        profile_size.label("profile_size"),
        topic_weight.c.weight.label("share"),
        topic_state.c.stability,
        topic_state.c.last_reviewed_at,
    )
    edges = (
        columns.select_from(topic)
        .join(course, course.c.id == topic.c.course_id)
        .join(coverage, coverage.c.topic_id == topic.c.id)
    )

    # Path 1: the edge points at the exam itself.
    by_exam = edges.join(assessment, assessment.c.id == coverage.c.assessment_id).join(
        assessment_slot, assessment_slot.c.id == assessment.c.slot_id
    )
    # Path 2: the edge points at a material; the exam is any of its course's with no profile.
    any_profile = exam_profile.alias("newer_profile")
    by_chapter = (
        edges.join(material, material.c.id == coverage.c.material_id)
        .join(assessment_slot, assessment_slot.c.course_id == course.c.id)
        .join(assessment, assessment.c.slot_id == assessment_slot.c.id)
        .where(~exists().where(any_profile.c.slot_id == assessment_slot.c.id))
    )

    # Taught so far (D-118): the topic has an edge to a taught chapter. Its own copy of
    # coverage - the outer query's coverage row is the edge this row came by, an exam edge on
    # path 1, and auto-correlation would test only that one edge.
    chapter_edge = coverage.alias("chapter_edge")
    taught = exists().where(
        chapter_edge.c.topic_id == topic.c.id,
        chapter_edge.c.material_id.in_(_taught_materials(user_id)),
    )
    # Exam prep (D-119): declared, and the day has come.
    in_prep = and_(assessment.c.prep_from.is_not(None), assessment.c.prep_from <= today.isoformat())

    def upcoming(paths: Select) -> Select:
        """The joins and filters both paths share, once they have reached an assessment."""
        return (
            # "latest" sits in the ON, not the WHERE: an exam with no profile keeps its row.
            paths.outerjoin(
                exam_profile,
                and_(
                    exam_profile.c.slot_id == assessment_slot.c.id,
                    exam_profile.c.version == latest_version,
                ),
            )
            # Same topic AND that profile - else v1's row joins too and the topic doubles.
            .outerjoin(
                topic_weight,
                and_(
                    topic_weight.c.exam_profile_id == exam_profile.c.id,
                    topic_weight.c.topic_id == topic.c.id,
                ),
            )
            .outerjoin(
                topic_state,
                and_(
                    topic_state.c.user_id == topic.c.user_id,
                    topic_state.c.topic_id == topic.c.id,
                ),
            )
            .where(
                topic.c.user_id == user_id,
                topic.c.status == "active",
                assessment_slot.c.kind == "exam",
                assessment.c.status == "upcoming",
                or_(
                    assessment.c.date.is_(None),
                    assessment.c.date >= today.isoformat(),  # YYYY-MM-DD text sorts as dates
                    assessment.c.date_approx == 1,
                ),
                or_(taught, in_prep) if taught_only else true(),
            )
        )

    both = union(upcoming(by_exam), upcoming(by_chapter))
    # ORDER BY sorts the whole union, by the columns' labels.
    query = both.order_by(both.selected_columns.topic_id, both.selected_columns.assessment_id)
    return list(conn.execute(query).all())


def _first_material(conn: Connection, user_id: str) -> dict[str, str]:
    """D-91's "chapter entered first" key (D-102): per topic, the earliest `added_at` over the
    materials it has an edge to. A topic with no material edge is absent - it sorts last.

    I3302 today, Chapter 6 tagged -> {MySQL: "2026-10-03T19:36:33Z"}."""
    rows = conn.execute(
        select(coverage.c.topic_id, func.min(material.c.added_at))
        .join_from(coverage, material, material.c.id == coverage.c.material_id)
        .where(coverage.c.user_id == user_id)
        .group_by(coverage.c.topic_id)
    )
    return dict(rows.tuples().all())


def _setup_items(conn: Connection, user_id: str, today: datetime.date) -> dict:
    """The "go get this input" items for today - the setup lane (D-15, D-22, D-85).

    Returns {"items": [one dict per item], "filtered": how many the snooze left out}.

    Each item says:
    - what is missing: `setup_kind`, one of the six kinds (e.g. `first-material`)
    - a fixed name for it: `setup_key` = `<kind>:<target_id>`, the same string in every plan
    - which course it belongs to
    - how many marks getting it could affect: `marks_unlocked_estimate` - the lane ranks by it

    Snooze: an item with study time logged against its key in the last SNOOZE_DAYS days (not
    voided) is left out - e.g. the student emailed the lecturer and is waiting for the reply.

    Live data 2026-10-09 -> 20 items:
    - 6 x `first-material`, one per course with no taught chapter (D-121) - I3350: 100 x 5 = 500
    - 14 x `past-papers`, one per exam slot with under 5 papers - I3350 Final: 70 x 5 = 350
    """
    cutoff = today - datetime.timedelta(days=SNOOZE_DAYS)
    snoozed = set(
        conn.execute(
            select(hours_entry.c.setup_key).where(
                hours_entry.c.user_id == user_id,
                hours_entry.c.setup_key.is_not(None),
                hours_entry.c.voided_at.is_(None),
                # "YYYY-MM-DDTHH:MM:SSZ" >= "YYYY-MM-DD" as text: exactly 7 days ago counts
                hours_entry.c.occurred_at >= cutoff.isoformat(),
            )
        )
        .scalars()
        .all()
    )

    # By id: later steps hold a course id (from an assessment or a slot) and need its credits.
    courses = {
        row.id: row
        for row in conn.execute(
            select(
                course.c.id,
                course.c.code,
                course.c.credits,
                course.c.credits_tier,
                course.c.target_grade,
            ).where(course.c.user_id == user_id)
        )
    }

    average_credits = _average_credits(row.credits for row in courses.values())

    # Step 3: six blocks, one per kind, each adding to `items` through `add`.
    # Every assessment once - grading and exam-date both read these rows.
    assessments = conn.execute(
        select(
            assessment.c.id,
            assessment.c.slot_id,
            assessment.c.weight,
            assessment.c.weight_tier,
            assessment.c.date,
            assessment.c.date_approx,
            assessment.c.status,
            assessment_slot.c.kind,
            assessment_slot.c.name.label("slot_name"),
            assessment_slot.c.course_id,
        )
        .join_from(assessment, assessment_slot, assessment_slot.c.id == assessment.c.slot_id)
        .where(assessment.c.user_id == user_id)
        .order_by(assessment.c.id)
    ).all()
    # Upcoming as in the study lane (D-95, D-99): still `upcoming`, and not past an exact date -
    # a sat exam whose status was never flipped asks for no papers and dates no tie.
    upcoming_exams = [
        r
        for r in assessments
        if r.kind == "exam"
        and r.status == "upcoming"
        and (r.date is None or r.date >= today.isoformat() or r.date_approx == 1)
    ]

    def weight_of(row) -> float:
        return row.weight if row.weight is not None else UNKNOWN_EXAM_WEIGHT  # D-96

    # Per course: its nearest exam date, the third tie key (D-91). Per slot: its course and
    # weight - a resit is a second row on the same slot, so the slot counts once.
    nearest_exam = {}
    slot_weight = {}
    for r in upcoming_exams:
        if r.date is not None and r.date < nearest_exam.get(r.course_id, "9999-12-31"):
            nearest_exam[r.course_id] = r.date
        _, weight, _ = slot_weight.get(r.slot_id, (r.course_id, 0.0, r.slot_name))
        slot_weight[r.slot_id] = (r.course_id, max(weight, weight_of(r)), r.slot_name)

    items = []

    def add(
        kind: str, target_id: str, course_id: str, marks: float, target_name: str | None = None
    ) -> None:
        """One item: its fixed key, and the marks it could move x the course's credits."""
        row = courses[course_id]
        if row.credits is not None:
            credits = row.credits
        else:  # D-84's average; none declared anywhere -> 1 (D-98)
            credits = average_credits if average_credits is not None else 1.0
        items.append(
            {
                "setup_kind": kind,
                "setup_key": f"{kind}:{target_id}",
                "target_name": target_name,  # the exam, for the reason; None for a course
                "course_id": course_id,
                "code": row.code,
                "credits": credits,
                "nearest_exam": nearest_exam.get(course_id),
                "marks_unlocked_estimate": marks * credits,
            }
        )

    # credits, target-grade: read the course rows from step 2; no marks of their own (D-85).
    for course_id, row in courses.items():
        if row.credits_tier == "unknown":
            add("credits", course_id, course_id, 0.0)
        if row.target_grade is None:
            add("target-grade", course_id, course_id, 0.0)

    # grading: one item per course, worth the sum of its guessed weights.
    guessed = {}
    for r in assessments:
        if r.weight_tier in ("unknown", "inferred"):
            guessed[r.course_id] = guessed.get(r.course_id, 0.0) + weight_of(r)
    for course_id, marks in guessed.items():
        add("grading", course_id, course_id, marks)

    # exam-date: one item per upcoming exam with no date, worth its own weight.
    for r in upcoming_exams:
        if r.date is None:
            add("exam-date", r.id, r.course_id, weight_of(r), r.slot_name)

    # first-material: a course with no taught chapter (D-121) - nothing in the study lane until
    # one is, so the plan asks for it. Covers the cold start too: no material, nothing taught.
    # Worth its upcoming exams' weight.
    taught_courses = set(
        conn.execute(
            select(material.c.course_id).where(
                material.c.user_id == user_id,
                material.c.id.in_(_taught_materials(user_id)),
            )
        ).scalars()
    )
    for course_id in courses:
        if course_id not in taught_courses:
            marks = sum(w for c, w, _ in slot_weight.values() if c == course_id)
            add("first-material", course_id, course_id, marks)

    # past-papers: an upcoming exam slot (D-99) under PAPERS_WANTED papers; worth its weight
    # until MIN_PAPERS land, then 0 - papers 4-5 unlock only the mock gate (D-24).
    papers = dict(
        conn.execute(
            select(past_exam.c.slot_id, func.count(past_exam.c.id))
            .where(past_exam.c.owner_id == user_id)
            .group_by(past_exam.c.slot_id)
        )
        .tuples()
        .all()
    )
    for slot_id, (course_id, weight, name) in slot_weight.items():
        count = papers.get(slot_id, 0)
        if count < PAPERS_WANTED:
            add("past-papers", slot_id, course_id, weight if count < MIN_PAPERS else 0.0, name)

    # Step 4: drop the snoozed keys before ranking, so ranks run 1..n with no gap.
    kept = [item for item in items if item["setup_key"] not in snoozed]

    # Step 5: the items, and how many the snooze dropped - the log line's "filtered".
    return {"items": kept, "filtered": len(items) - len(kept)}


def _due_reviews(conn: Connection, user_id: str, today: datetime.date) -> list:
    """Topics whose review is due (`due_at` passed, topic `active`), most overdue first (D-87).
    Live data today -> [] (0 `topic_state` rows until 1.16).

    Due means `due_at` falls on `today` or earlier: it is a UTC timestamp, so anything before
    tomorrow's date as text. An `unexamined` topic is not reviewed (D-15); a topic on a sat exam
    still is - the review lane is how it gets revised (D-94).
    """
    tomorrow = (today + datetime.timedelta(days=1)).isoformat()
    return list(
        conn.execute(
            select(
                topic.c.id.label("topic_id"),
                topic.c.name.label("topic_name"),
                course.c.id.label("course_id"),
                course.c.code,
                topic_state.c.due_at,
            )
            .join_from(topic_state, topic, topic.c.id == topic_state.c.topic_id)
            .join(course, course.c.id == topic.c.course_id)
            .where(
                topic_state.c.user_id == user_id,
                topic.c.status == "active",
                topic_state.c.due_at.is_not(None),
                topic_state.c.due_at < tomorrow,
            )
            # Most overdue first; one moment for two topics falls back to the id.
            .order_by(topic_state.c.due_at, topic.c.id)
        ).all()
    )


def get_plan(
    engine: Engine,
    user_id: str,
    today: datetime.date,
    shown_n: int = SHOWN_N,
    tz: datetime.tzinfo | None = None,
) -> dict:
    """Rank every course's work into three lanes and record it, as one write unit (D-12).

    Writes one `plan` (scope `all-courses`, `shown_n`, `strategy_snapshot` with one key per
    course) and one `plan_item` per item in every lane, ranks 1..n per lane, ties broken by
    D-91 and then the topic id or setup key (D-100); the top `shown_n` of each lane get
    `shown = 1`. Logs one line after the commit: candidates, filtered (and how many of
    them are untaught, D-123), lane counts, run time.

    Returns {"plan_id", "lanes": {"review" | "study" | "setup": {"items": [the shown items, in
    full], "hidden": count}}} - only what is returned is marked shown (D-92).

    Refuses `shown_n` below 1 before anything is read. Weakness is 1.0 for a topic never
    practised, else 1 - its FSRS recall chance on `today`, days since its review counted in `tz`
    (None: this machine's zone, the one `today` is taken in) (D-82).

    Live data 2026-10-09 (a copy, D-122), shown_n 5 -> study: MySQL (4.249), frame hex decoding
    (0.652), IP addressing (0.367), protocol layers (0.029) - the taught chapters' topics only,
    21 filtered · setup: first-material I3350 (500), I3301, I3303 (400 each), past-papers I3350
    Final (350), first-material DHR300 (300), 15 hidden · review: none.
    """
    started = time.perf_counter()
    if isinstance(shown_n, bool) or not isinstance(shown_n, int) or shown_n < 1:
        raise invalid(
            "shown_n",
            f"{shown_n!r} is not a whole number of at least 1",
            "send how many items of each lane to show, e.g. 5",
        )

    with write_unit(engine) as conn:
        # Gather: every read sits inside the unit, so the plan comes from one moment.
        courses = conn.execute(
            select(
                course.c.id,
                course.c.credits,
                course.c.strategy,
                course.c.conceded,
                course.c.target_grade,
            ).where(course.c.user_id == user_id)
        ).all()
        average_credits = _average_credits(row.credits for row in courses)
        snapshot = {
            row.id: {
                "strategy": row.strategy,
                "conceded": bool(row.conceded),
                "target_grade": row.target_grade,
            }
            for row in courses
        }
        latest = conn.execute(
            select(func.max(assessment.c.date))
            .join_from(assessment, assessment_slot, assessment_slot.c.id == assessment.c.slot_id)
            .where(assessment.c.user_id == user_id, assessment_slot.c.kind == "exam")
        ).scalar()
        latest_exam_date = datetime.date.fromisoformat(latest) if latest else None

        # Study lane.
        topics = _topic_exams(
            _study_rows(conn, user_id, today), today, average_credits, latest_exam_date
        )
        first_material = _first_material(conn, user_id)
        study = []
        for entry in topics.values():
            if entry["stability"] is None:
                weakness = COLD_WEAKNESS  # never practised (D-82)
            else:
                recall = retrievability(entry["stability"], entry["last_reviewed_at"], today, tz)
                weakness = 1 - recall
            pairs = [(exam["marks"], exam["days"]) for exam in entry["exams"]]
            study.append(
                {
                    "topic_id": entry["topic_id"],
                    "topic_name": entry["topic_name"],
                    "course_id": entry["course_id"],
                    "code": entry["code"],
                    "credits": entry["credits"],
                    "nearest_days": min(exam["days"] for exam in entry["exams"]),
                    "first_material": first_material.get(entry["topic_id"]),  # None: no chapter
                    "score": study_score(pairs, weakness, ESTIMATED_MINUTES),
                    "estimated_minutes": ESTIMATED_MINUTES,
                    "exam_profile_id": entry["exam_profile_id"],
                    "reason": study_reason(entry, weakness, ESTIMATED_MINUTES),
                }
            )
        active = conn.execute(
            select(func.count(topic.c.id)).where(
                topic.c.user_id == user_id, topic.c.status == "active"
            )
        ).scalar_one()
        # The two reasons an active topic is left out, counted apart for the log (D-123):
        # no upcoming exam (D-94), or not taught yet with its exams in term (D-118). The rows
        # without the taught filter hold every topic that has an upcoming exam.
        reachable = len(
            {row.topic_id for row in _study_rows(conn, user_id, today, taught_only=False)}
        )
        untaught = reachable - len(study)
        no_exam_left = active - reachable

        # Setup lane.
        setup = _setup_items(conn, user_id, today)
        setup_lane = [dict(item, reason=setup_reason(item)) for item in setup["items"]]

        # Review lane: already most overdue first. The schema needs a score off the setup lane,
        # so a review's score is how many days overdue it is - the order the lane already uses.
        review_lane = [
            {
                "topic_id": row.topic_id,
                "topic_name": row.topic_name,
                "course_id": row.course_id,
                "code": row.code,
                "due_at": row.due_at,
                "score": float((today - datetime.date.fromisoformat(row.due_at[:10])).days),
                "reason": review_reason(row),
            }
            for row in _due_reviews(conn, user_id, today)
        ]

        # Rank: each lane on its own; ties by D-91, then the id, so every rank is unique.
        lanes = {
            "review": review_lane,
            "study": sorted(
                study,
                key=lambda i: (
                    -i["score"],
                    -i["credits"],
                    # the chapter entered first; a topic with no chapter after all (D-102)
                    i["first_material"] is None,
                    i["first_material"] or "",
                    i["nearest_days"],
                    i["code"],
                    i["topic_id"],
                ),
            ),
            "setup": sorted(
                setup_lane,
                key=lambda i: (
                    -i["marks_unlocked_estimate"],
                    -i["credits"],
                    i["nearest_exam"] is None,  # an undated course after every dated one
                    i["nearest_exam"] or "",
                    i["code"],
                    i["setup_key"],
                ),
            ),
        }
        for items in lanes.values():
            for rank, item in enumerate(items, start=1):
                item["rank"] = rank
                item["shown"] = rank <= shown_n

        # Write: the plan, then every item of every lane - the full ranking (D-12, D-22).
        plan_id = new_id()
        conn.execute(
            insert(plan).values(
                id=plan_id,
                user_id=user_id,
                scope="all-courses",
                shown_n=shown_n,
                strategy_snapshot=json.dumps(snapshot),
                created_at=now(),
            )
        )
        rows = [
            {
                "id": new_id(),
                "user_id": user_id,
                "plan_id": plan_id,
                "lane": lane,
                "course_id": item["course_id"],
                "topic_id": item.get("topic_id"),
                "setup_kind": item.get("setup_kind"),
                "setup_key": item.get("setup_key"),
                "rank": item["rank"],
                "shown": int(item["shown"]),
                "score": item.get("score"),
                "reason": item["reason"],
                "estimated_minutes": item.get("estimated_minutes"),
                "marks_unlocked_estimate": item.get("marks_unlocked_estimate"),
                "exam_profile_id": item.get("exam_profile_id"),
            }
            for lane, items in lanes.items()
            for item in items
        ]
        if rows:
            conn.execute(insert(plan_item), rows)

    # Report: after the commit, so only a saved plan is logged.
    filtered = setup["filtered"] + no_exam_left + untaught
    log.info(
        json.dumps(
            {
                "event": "get_plan",
                "plan_id": plan_id,
                "candidates": len(rows) + filtered,
                "filtered": filtered,
                "untaught": untaught,  # of `filtered`: topics not taught yet, exams in term
                "lanes": {lane: len(items) for lane, items in lanes.items()},
                "ms": round((time.perf_counter() - started) * 1000),
            }
        )
    )
    return {
        "plan_id": plan_id,
        "lanes": {
            lane: {
                "items": [_shown(item) for item in items if item["shown"]],
                "hidden": sum(1 for item in items if not item["shown"]),
            }
            for lane, items in lanes.items()
        },
    }


# What the host reads of one item: what to show, and the ids it needs to log time against it.
SHOWN_FIELDS = (
    "rank",
    "code",
    "topic_id",
    "topic_name",
    "setup_kind",
    "setup_key",
    "target_name",
    "score",
    "marks_unlocked_estimate",
    "estimated_minutes",
    "due_at",
    "reason",
)


def _shown(item: dict) -> dict:
    return {field: item[field] for field in SHOWN_FIELDS if field in item}
