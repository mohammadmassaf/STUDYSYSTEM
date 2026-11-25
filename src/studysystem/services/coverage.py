"""Coverage-at-exam (4.6; D-12, D-268 - D-271): per course, the share of its nearest exam that
the student has reached, stored with its denominator tier at every plan run.

A course 25 days before its Partial (profile v1): reached shares 66.1 of 99.8 = 66.2%,
tier `weighted`, note "Partial exam: 7 profile; 0 unexamined topics excluded".
"""

from collections import defaultdict

from sqlalchemy import Connection, func, insert, select

from studysystem.db.tables import coverage_reading, topic
from studysystem.services.ids import new_id, now
from studysystem.services.unconfirmed import unreached_topics

# A share's source (from `plan._topic_exams`) -> the tier it counts toward (D-271).
SOURCE_TIER = {
    "profile": "weighted",  # real papers
    "stated": "weighted",  # the instructor's word
    "average": "inferred",  # a topic missing from the profile, guessed (D-89)
    "equal split": "unweighted",  # a topic count
}
# How strong each tier is: a tie goes to the weaker one (D-271).
TIER_STRENGTH = {"weighted": 2, "inferred": 1, "unweighted": 0}


def course_reading(entries: list[dict], unreached: set[str], unexamined: int) -> dict | None:
    """One course's reading, or None when no topic of the course reaches an upcoming exam.

    `entries`: the course's topics as `plan._topic_exams` returns them - each has "topic_id" and
    "exams", a list of {"assessment_id", "exam_name", "days", "share", "share_source", ...}.
    `unreached`: topic ids nothing has reached (`unconfirmed.unreached_topics`, D-266).
    `unexamined`: how many of the course's topics are `unexamined` - for the note only (D-13).

    Returns {"value": 0-1, "denominator_tier": ..., "tier_note": ...}.

    I3304 -> Partial (25 days) beats Final (67 days); reached 43.2 + 7.8 + 7.2 + 5.9 + 2.0 = 66.1
    of 99.8 -> value 0.662, `weighted`, "Partial exam: 7 profile; 0 unexamined topics excluded".
    I3305, nothing reached -> value 0.0 (a real 0%, not unknown).
    DHR300, no topics -> None.
    """
    # 1. Nearest exam: (days, name) per exam id. Same days -> the name decides, so every run
    #    picks the same one.
    exams = {}
    for entry in entries:
        for exam in entry["exams"]:
            exams[exam["assessment_id"]] = (exam["days"], exam["exam_name"])
    if not exams:
        return None  # DHR300: no topic on any upcoming exam
    earliest_exam = min(exams, key=lambda exam_id: exams[exam_id])
    _, exam_name = exams[earliest_exam]

    # 2 + 3. Only that exam's shares: the fraction, the totals per tier, the topics per source.
    totals = defaultdict(float)
    sources = defaultdict(int)
    top = bottom = 0.0
    for entry in entries:
        for exam in entry["exams"]:
            if exam["assessment_id"] != earliest_exam:
                continue  # OSPF's Final entry, when the Partial is nearer
            totals[SOURCE_TIER[exam["share_source"]]] += exam["share"]
            sources[exam["share_source"]] += 1
            bottom += exam["share"]
            if entry["topic_id"] not in unreached:
                top += exam["share"]
    best = max(totals, key=lambda t: (totals[t], -TIER_STRENGTH[t]))

    # 4. The note: one count per source, in SOURCE_TIER's order.
    mix = ", ".join(f"{sources[s]} {s}" for s in SOURCE_TIER if s in sources)
    excluded = f"{unexamined} unexamined topic{'' if unexamined == 1 else 's'} excluded"
    return {
        # Every share 0 (a profile or a pin can give 0) -> 0%, not a crash in the plan run (D-272).
        "value": top / bottom if bottom else 0.0,
        "denominator_tier": best,
        "tier_note": f"{exam_name}: {mix}; {excluded}",
    }


def write_readings(
    conn: Connection, user_id: str, topics: dict, skip: set[str], stamp: str | None = None
) -> None:
    """Insert one `coverage_reading` per course with a reading, inside the plan run's write unit
    (D-268). Insert-only: an older reading is never changed (D-12).

    `topics`: `plan._topic_exams` over every row before the taught filter - coverage counts a
    topic not taught yet too. `skip`: course ids the plan leaves out (dropped, never planned,
    D-216, D-217) - `_study_rows` still returns their topics. `stamp`: the `computed_at` to
    write, None = now (D-308: only `get_plan`'s demo-seed clock passes it).
    """
    stamp = stamp or now()
    unreached = {row.id for row in conn.execute(unreached_topics(user_id))}
    unexamined = {
        row.course_id: row.n
        for row in conn.execute(
            select(topic.c.course_id, func.count(topic.c.id).label("n"))
            .where(topic.c.user_id == user_id, topic.c.status == "unexamined")
            .group_by(topic.c.course_id)
        )
    }
    by_course: dict[str, list[dict]] = defaultdict(list)
    for entry in topics.values():
        by_course[entry["course_id"]].append(entry)

    rows = []
    for course_id, entries in by_course.items():
        if course_id in skip:
            continue
        reading = course_reading(entries, unreached, unexamined.get(course_id, 0))
        if reading is None:
            continue
        rows.append(
            {
                "id": new_id(),
                "user_id": user_id,
                "course_id": course_id,
                **reading,
                "computed_at": stamp,
            }
        )
    if rows:
        conn.execute(insert(coverage_reading), rows)
