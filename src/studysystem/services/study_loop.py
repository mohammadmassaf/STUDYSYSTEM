"""The study loop (1.16; D-17, D-22, D-82): sessions label the work, hours are explicit
assertions, and attempts move `topic_state`.

`topic_state` is a replay: a topic's attempts in, its FSRS state out, rebuilt from every attempt
each time. A voided attempt is undone by replaying without it (`study_reject_item`'s "topic_state
recomputed"), so the state is never patched one attempt at a time.
"""

import datetime
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass

from fsrs import Card, Rating, Scheduler
from sqlalchemy import Connection, Engine, delete, func, insert, select, update

from studysystem.db.tables import (
    assessment_slot,
    hours_entry,
    material,
    past_exam,
    plan_item,
    practice_item,
    study_session,
    topic,
    topic_state,
)
from studysystem.db.tables import (
    attempt as attempt_table,
)
from studysystem.errors import StudyError
from studysystem.services import values
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_course
from studysystem.services.units import write_unit

log = logging.getLogger(__name__)

MODES = ("practice", "exam-walkthrough", "explain-chapter", "mock", "review-quiz")
SUBJECT_TYPES = ("topic", "material", "past-exam")
SESSION_HOURS = 4  # an open session expires this long after it starts and records nothing (D-17)
TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"  # the schema's timestamp format, as `now()` writes it

PASS_SCORE = 0.5  # a day scoring above this is Good, at or under it Again - D-99's "mostly wrong"

# FSRS with no learning steps: a topic is reviewed in days, never re-shown minutes later, so every
# reviewed card sits in Review state and `topic_state` needs no state or step column. Fuzzing
# off: the same attempts always give the same due date.
SCHEDULER = Scheduler(learning_steps=(), relearning_steps=(), enable_fuzzing=False)


def retrievability(
    stability: float, last_reviewed_at: str, today: datetime.date, tz: datetime.tzinfo | None
) -> float:
    """FSRS's chance he still recalls the topic on `today`, 0-1 (pure; D-82, D-88).

    Days since the review count in whole local days, as the replay groups them: reviewed Oct 6
    at 20:10 in Beirut -> Oct 7 is 1 day later, whatever the hour. The package counts whole
    24-hour spans instead (Oct 7 at 19:30 would still be 0 days), so it is handed the review
    moment plus exactly that many days. A `today` before the review counts as 0 days. `tz` None
    is this machine's zone - the one `today` is taken in.

    Stability 2.31 (one Good), 1 day later -> 0.947; 10 days -> 0.774.
    """
    last = datetime.datetime.fromisoformat(last_reviewed_at)
    days = max(0, (today - last.astimezone(tz).date()).days)
    card = Card(stability=stability, last_review=last)
    return SCHEDULER.get_card_retrievability(card, last + datetime.timedelta(days=days))


@dataclass(frozen=True)
class AttemptRow:
    """One attempt on the topic, as the replay reads it. A SQL row with these names works too."""

    created_at: str  # UTC, "YYYY-MM-DDTHH:MM:SSZ"
    assisted: bool
    correctness_voided: bool
    correct: bool | None
    score: float | None  # 0-1; at least one of correct, score is set
    marks: float | None  # the item's marks; None when the paper gave none, or there is no item


def replay_topic(
    attempts: Iterable[AttemptRow], average_marks: float | None, tz: datetime.tzinfo | None
) -> dict | None:
    """A topic's FSRS state from all its attempts - one review per local day (pure; D-88).

    Takes every attempt on the topic (any order), the average of the topic's known item marks
    (None when it has none) and the student's timezone. Returns None when no attempt counts - the
    caller then deletes the topic's row - else {"stability", "difficulty", "due_at",
    "last_reviewed_at", "reps", "lapses"}, the two times as UTC datetimes.

    Rules:
    - counts: unassisted, not correctness-voided attempts only
    - one review per local day, at the day's last counted attempt; days reach FSRS oldest first
    - day score = marks-weighted average; an unknown mark borrows `average_marks`; with that None
      too, every part weighs the same
    - an attempt's value is its score when set (it wins over `correct`), else 1.0 if correct
    - day score > PASS_SCORE -> Rating.Good, else Rating.Again
    - reps = days reviewed, lapses = days rated Again

    Examples, MySQL, Beirut (UTC+3):
    - Oct 6, index.php: 5 ok, 5 at 0.4, 15 ok, 25 wrong -> 22/50 = 0.44 -> one Again at the last
      part's time; reps 1, lapses 1
    - same, plus 2023 Q III (35 marks, ok) at 20:10 -> 57/85 = 0.67 -> one Good at 20:10
    - Oct 9, Problem V pos 5 ok, pos 6 wrong, both marks unknown -> 0.5 -> Again
    - every attempt assisted or voided -> None
    """

    valid_attempts = []
    for attempt in attempts:
        if attempt.assisted or attempt.correctness_voided:
            continue
        valid_attempts.append(attempt)
    if not valid_attempts:
        return None

    # Group by local date: each day collects its counted attempts (Oct 6 in Beirut -> 5).
    days = {}
    for attempt in valid_attempts:
        moment = datetime.datetime.fromisoformat(attempt.created_at)
        days.setdefault(moment.astimezone(tz).date(), []).append(attempt)

    # One review per day, oldest first, on one card: the card carries MySQL's memory across days.
    card = Card()
    reps = lapses = 0
    for date in sorted(days):
        earned = total = plain = 0.0
        latest = ""
        for attempt in days[date]:
            if attempt.score is not None:
                value = attempt.score
            else:
                value = 1.0 if attempt.correct else 0.0
            if attempt.marks is not None:
                weight = attempt.marks
            elif average_marks is not None:
                weight = average_marks
            else:
                weight = 1.0  # no known marks on the topic: every part weighs the same
            earned += value * weight
            total += weight
            plain += value
            latest = max(latest, attempt.created_at)  # one text format: the largest is the latest
        # A day of 0-mark items only has no weight to share: every part counts the same.
        if total > 0:
            day_score = earned / total
        else:
            day_score = plain / len(days[date])
        rating = Rating.Good if day_score > PASS_SCORE else Rating.Again
        card, _ = SCHEDULER.review_card(card, rating, datetime.datetime.fromisoformat(latest))
        reps += 1
        if rating == Rating.Again:
            lapses += 1

    return {
        "stability": card.stability,
        "difficulty": card.difficulty,
        "due_at": card.due,
        "last_reviewed_at": card.last_review,
        "reps": reps,
        "lapses": lapses,
        "last_day": {"score": round(day_score, 3), "rating": rating.name},
    }


# --- sessions: label the work, never minutes (D-17) ---------------------------------------


def _not_found(field: str, what: str, fix: str) -> StudyError:
    return StudyError(
        code="not_found",
        message=f"no {what}",
        fix=fix,
        field_errors=[{"field": field, "problem": f"no {what}"}],
    )


def _subject_course(conn: Connection, user_id: str, subject_type: str, subject_id: str):
    """The course of the session's subject, or None when this user has no such row."""
    if subject_type == "topic":
        query = select(topic.c.course_id).where(
            topic.c.id == subject_id, topic.c.user_id == user_id
        )
    elif subject_type == "material":
        query = select(material.c.course_id).where(
            material.c.id == subject_id, material.c.user_id == user_id
        )
    else:  # past-exam: the paper's course is its slot's
        query = (
            select(assessment_slot.c.course_id)
            .join_from(past_exam, assessment_slot, assessment_slot.c.id == past_exam.c.slot_id)
            .where(past_exam.c.id == subject_id, past_exam.c.owner_id == user_id)
        )
    return conn.execute(query).scalar_one_or_none()


def start_session(
    engine: Engine,
    user_id: str,
    code: str,
    mode: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
    semester_name: str | None = None,
) -> dict:
    """Open one study session on a course: what kind of work, about what. Writes one row and
    never a minute - an open session expires 4 hours after it starts, unrecorded (D-17).

    The subject is optional, but given it is both halves, and it must be this course's topic,
    material or past paper (Same course invariant). Tonight: I3302, practice, topic MySQL.

    Returns {"session_id", "mode", "subject_type", "subject_id", "started_at", "expires_at"}.
    """
    mode = values.choice("mode", mode, MODES)
    if (subject_type is None) != (subject_id is None):
        raise values.invalid(
            "subject_id" if subject_id is None else "subject_type",
            "a subject is a type and an id together",
            "send both subject_type and subject_id, or neither",
        )
    if subject_type is not None:
        subject_type = values.choice("subject_type", subject_type, SUBJECT_TYPES)

    started = datetime.datetime.now(datetime.UTC)
    session_id = new_id()
    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        if subject_type is not None and subject_id is not None:
            found = _subject_course(conn, user_id, subject_type, subject_id)
            if found != course_id:  # None too: no such row of this user's
                raise _not_found(
                    "subject_id",
                    f"{subject_type} with this id in {code}",
                    f"use an id from study_get_topics for {code}",
                )
        row = {
            "started_at": started.strftime(TIMESTAMP),
            "expires_at": (started + datetime.timedelta(hours=SESSION_HOURS)).strftime(TIMESTAMP),
        }
        conn.execute(
            insert(study_session).values(
                id=session_id,
                user_id=user_id,
                course_id=course_id,
                mode=mode,
                subject_type=subject_type,
                subject_id=subject_id,
                source="system",
                **row,
            )
        )

    return {
        "session_id": session_id,
        "mode": mode,
        "subject_type": subject_type,
        "subject_id": subject_id,
        **row,
    }


# --- hours: the only place minutes come from (D-17, D-22) ---------------------------------


def _minutes(value: object) -> int:
    nb = values.number("minutes", value, ge=1, le=720)
    if nb != int(nb):
        raise values.invalid("minutes", f"{nb:g} is not a whole number", "send whole minutes")
    return int(nb)


def log_hours(
    engine: Engine,
    user_id: str,
    code: str,
    minutes: object = None,
    topic_id: str | None = None,
    setup_key: str | None = None,
    session_id: str | None = None,
    voids: str | None = None,
    semester_name: str | None = None,
) -> dict:
    """Assert the minutes studied on a course - the only way minutes enter (D-17). One write unit:
    the new `hours_entry`, the entry it `voids`, and the session it closes.

    Scope is a topic, a setup item's key, or neither (course-level, the cold-start case) - never
    both. A setup key must be one a plan has raised for this course: a mistyped key would
    snooze nothing (D-22). A session it names must be this course's; a still-open one is ended
    now. A mistyped entry is never edited: send `voids` with its id - and the corrected minutes,
    or none to only void it. The work started at the session's start, or `minutes` ago.

    Returns {"hours_entry_id" (None when only voiding), "minutes", "voided"}.
    """
    if minutes is None and voids is None:
        raise values.invalid("minutes", "is missing", "send the minutes studied, 1-720")
    scoped = [f for f, v in (("topic_id", topic_id), ("setup_key", setup_key)) if v is not None]
    if minutes is None and (scoped or session_id is not None):
        raise values.invalid(
            (scoped or ["session_id"])[0],
            "voiding alone writes no entry, so it takes no topic, setup key or session",
            "send the corrected minutes with them, or send voids alone",
        )
    if minutes is not None:
        minutes = _minutes(minutes)
    if topic_id is not None and setup_key is not None:
        raise values.invalid(
            "setup_key",
            "hours go to a topic or a setup item, not both",
            "send topic_id or setup_key, or neither for course-level hours",
        )

    stamp = now()
    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        voided = _void(conn, user_id, course_id, voids, stamp) if voids is not None else None
        if minutes is None:
            return {"hours_entry_id": None, "minutes": None, "voided": voided}

        if topic_id is not None and _subject_course(conn, user_id, "topic", topic_id) != course_id:
            raise _not_found(
                "topic_id",
                f"topic with this id in {code}",
                f"use an id from study_get_topics for {code}",
            )
        if setup_key is not None:
            raised = conn.execute(
                select(plan_item.c.id)
                .where(
                    plan_item.c.user_id == user_id,
                    plan_item.c.course_id == course_id,
                    plan_item.c.setup_key == setup_key,
                )
                .limit(1)
            ).first()
            if raised is None:
                raise _not_found(
                    "setup_key",
                    f"setup item {setup_key!r} in {code}'s plans",
                    "use a setup_key exactly as study_get_plan returned it",
                )

        started = None
        if session_id is not None:
            session = conn.execute(
                select(
                    study_session.c.started_at, study_session.c.expires_at, study_session.c.ended_at
                ).where(
                    study_session.c.id == session_id,
                    study_session.c.user_id == user_id,
                    study_session.c.course_id == course_id,
                )
            ).first()
            if session is None:
                raise _not_found(
                    "session_id",
                    f"session with this id in {code}",
                    "use the id study_start_session returned",
                )
            started = session.started_at
            if session.ended_at is None and stamp < session.expires_at:
                conn.execute(
                    update(study_session)
                    .where(study_session.c.id == session_id)
                    .values(ended_at=stamp)
                )
        if started is None:
            began = datetime.datetime.fromisoformat(stamp) - datetime.timedelta(minutes=minutes)
            started = began.strftime(TIMESTAMP)

        entry_id = new_id()
        conn.execute(
            insert(hours_entry).values(
                id=entry_id,
                user_id=user_id,
                course_id=course_id,
                session_id=session_id,
                topic_id=topic_id,
                setup_key=setup_key,
                minutes=minutes,
                occurred_at=started,
                source="tool",
                created_at=stamp,
            )
        )

    return {"hours_entry_id": entry_id, "minutes": minutes, "voided": voided}


def _void(conn: Connection, user_id: str, course_id: str, entry_id: str, stamp: str) -> str:
    """Stamp `voided_at` on this user's live entry on this course; returns its id."""
    done = conn.execute(
        update(hours_entry)
        .where(
            hours_entry.c.id == entry_id,
            hours_entry.c.user_id == user_id,
            hours_entry.c.course_id == course_id,
            hours_entry.c.voided_at.is_(None),
        )
        .values(voided_at=stamp)
    )
    if done.rowcount != 1:
        raise _not_found(
            "voids", "live hours entry with this id", "send the id of an entry not voided yet"
        )
    return entry_id


# --- attempts: what moves `topic_state` ---------------------------------------------------

# What an attempt in each session mode is (D-07b): its `source`, and whether it was assisted.
# An explanation's check questions come right after the explanation, so they are assisted and
# build the mistake log without moving FSRS; a mock's answers count as unassisted practice.
MODE_SOURCE = {
    "practice": ("practice", False),
    "mock": ("practice", False),
    "review-quiz": ("review-quiz", False),
    "exam-walkthrough": ("exam-walkthrough", True),
    "explain-chapter": ("comprehension-check", True),
}
ROOT_CAUSES = ("concept", "recall", "procedure", "misread", "careless", "time-pressure")
PRACTISABLE = ("active", "unexamined")  # a proposed topic stays declinable: no state (D-22)


def _attempt_values(raw: object, n: int) -> dict:
    """One submitted attempt, checked: {practice_item_id, topic_id, correct, score, root_cause,
    fix_rule}. The item and topic are looked up later, inside the unit."""
    where = f"attempts[{n}]"
    if not isinstance(raw, dict):
        raise values.invalid(where, f"{raw!r} is not an object", "send each attempt as an object")
    item_id, topic_id = raw.get("practice_item_id"), raw.get("topic_id")
    if item_id is None and topic_id is None:
        raise values.invalid(
            f"{where}.practice_item_id",
            "an attempt names the question, or at least its topic",
            "send practice_item_id from study_get_topics, or topic_id for a question with no item",
        )
    correct = raw.get("correct")
    score = raw.get("score")
    if correct is None and score is None:
        raise values.invalid(
            f"{where}.score", "an attempt needs a result", "send score (0-1), correct, or both"
        )
    root_cause = raw.get("root_cause")
    fix_rule = raw.get("fix_rule")
    return {
        "practice_item_id": item_id,
        "topic_id": topic_id,
        "correct": None if correct is None else values.flag(f"{where}.correct", correct),
        "score": None if score is None else values.number(f"{where}.score", score, ge=0, le=1),
        "root_cause": None
        if root_cause is None
        else values.choice(f"{where}.root_cause", root_cause, ROOT_CAUSES),
        "fix_rule": None if fix_rule is None else values.text(f"{where}.fix_rule", fix_rule),
    }


def _attempt_topic(conn: Connection, user_id: str, course_id: str, code: str, a: dict, n: int):
    """The topic the attempt lands on - its item's, when it names one (Same course) - and the
    item's paper and position. Refuses another course's item or topic, and a topic not open to
    practice."""
    where = f"attempts[{n}]"
    paper = position = None
    topic_id = a["topic_id"]
    if a["practice_item_id"] is not None:
        item = conn.execute(
            select(
                practice_item.c.topic_id, practice_item.c.past_exam_id, practice_item.c.position
            ).where(practice_item.c.id == a["practice_item_id"], practice_item.c.user_id == user_id)
        ).first()
        if item is None:
            raise _not_found(
                f"{where}.practice_item_id", "item with this id", "use an id from study_get_topics"
            )
        if topic_id is not None and topic_id != item.topic_id:
            raise values.invalid(
                f"{where}.topic_id",
                "is not the item's topic",
                "leave topic_id out: the item's topic is used",
            )
        topic_id, paper, position = item.topic_id, item.past_exam_id, item.position
    found = conn.execute(
        select(topic.c.course_id, topic.c.status).where(
            topic.c.id == topic_id, topic.c.user_id == user_id
        )
    ).first()
    if found is None or found.course_id != course_id:
        sent = "practice_item_id" if a["practice_item_id"] is not None else "topic_id"
        raise _not_found(
            f"{where}.{sent}",
            f"{'item' if sent == 'practice_item_id' else 'topic'} with this id in {code}",
            f"use an id from study_get_topics for {code}",
        )
    if found.status not in PRACTISABLE:
        raise StudyError(
            code="topic_not_practisable",
            message=f"{where}: the topic is {found.status}",
            fix="confirm the topic with study_confirm_topic_proposal first"
            if found.status == "proposed"
            else "attempt the topic that replaced it",
            field_errors=[{"field": f"{where}.topic_id", "problem": f"topic is {found.status}"}],
        )
    return topic_id, paper, position


def _session_for_attempts(conn: Connection, user_id: str, course_id: str, code, session_id, stamp):
    """The session the attempts belong to: the one named, else this course's latest open one,
    else a new `manual` practice session (Write units). Returns its row."""
    columns = (
        study_session.c.id,
        study_session.c.mode,
        study_session.c.subject_type,
        study_session.c.subject_id,
        study_session.c.position,
    )
    if session_id is not None:
        session = conn.execute(
            select(*columns).where(
                study_session.c.id == session_id,
                study_session.c.user_id == user_id,
                study_session.c.course_id == course_id,
            )
        ).first()
        if session is None:
            raise _not_found(
                "session_id",
                f"session with this id in {code}",
                "use the id study_start_session returned",
            )
        return session
    session = conn.execute(
        select(*columns)
        .where(
            study_session.c.user_id == user_id,
            study_session.c.course_id == course_id,
            study_session.c.ended_at.is_(None),
            study_session.c.expires_at > stamp,
        )
        .order_by(study_session.c.started_at.desc(), study_session.c.id.desc())
        .limit(1)
    ).first()
    if session is not None:
        return session
    started = datetime.datetime.fromisoformat(stamp)
    new = new_id()
    conn.execute(
        insert(study_session).values(
            id=new,
            user_id=user_id,
            course_id=course_id,
            mode="practice",
            source="manual",
            started_at=stamp,
            expires_at=(started + datetime.timedelta(hours=SESSION_HOURS)).strftime(TIMESTAMP),
        )
    )
    return conn.execute(select(*columns).where(study_session.c.id == new)).one()


def _store_state(conn: Connection, user_id: str, topic_id: str, tz: datetime.tzinfo | None):
    """Replay the topic's attempts and write the result: insert, update, or delete when nothing
    counts (his pick - a state that exists only while an attempt counts). Returns the state."""
    rows = conn.execute(
        select(
            attempt_table.c.created_at,
            attempt_table.c.assisted,
            attempt_table.c.correctness_voided,
            attempt_table.c.correct,
            attempt_table.c.score,
            practice_item.c.marks,
        )
        .join_from(
            attempt_table,
            practice_item,
            practice_item.c.id == attempt_table.c.practice_item_id,
            isouter=True,
        )
        .where(attempt_table.c.user_id == user_id, attempt_table.c.topic_id == topic_id)
    ).all()
    average = conn.execute(
        select(func.avg(practice_item.c.marks)).where(
            practice_item.c.topic_id == topic_id,
            # every known mark, rejected items too: their attempts still count with their own
            # marks (D-16), so the stand-in for an unknown one comes from the same pool (D-112)
            practice_item.c.marks.is_not(None),
        )
    ).scalar()
    attempts = [AttemptRow(**row._mapping) for row in rows]
    state = replay_topic(attempts, None if average is None else float(average), tz)

    key = (topic_state.c.user_id == user_id) & (topic_state.c.topic_id == topic_id)
    if state is None:
        conn.execute(delete(topic_state).where(key))
        return None
    last_day = state.pop("last_day")
    stored = {
        **state,
        "due_at": state["due_at"].strftime(TIMESTAMP),
        "last_reviewed_at": state["last_reviewed_at"].strftime(TIMESTAMP),
        "inferred": False,
        "updated_at": now(),
    }
    if conn.execute(select(topic_state.c.topic_id).where(key)).first() is None:
        conn.execute(insert(topic_state).values(user_id=user_id, topic_id=topic_id, **stored))
    else:
        conn.execute(update(topic_state).where(key).values(**stored))
    return {**stored, "last_day": last_day}


def submit_attempts(
    engine: Engine,
    user_id: str,
    code: str,
    attempts: object,
    session_id: str | None = None,
    semester_name: str | None = None,
    tz: datetime.tzinfo | None = None,
) -> dict:
    """Record one graded problem - its parts, together - and move each topic's FSRS state. One
    write unit: the attempts, every touched `topic_state`, the walkthrough's position, and a
    `manual` session when none is open (Write units).

    The session's mode decides each attempt's `source` and `assisted` (MODE_SOURCE). Each
    attempt names its item (its topic follows) or only a topic; a result is a score 0-1,
    correct, or both - the score wins in the replay. Tonight: index.php's four parts in one call.

    Returns {"session_id", "attempt_ids", "topics": [{"topic_id", "due_at", "reps", "lapses"} or
    {"topic_id", "state": None} when nothing counts]}. Logs one line after the commit: each
    topic's last day score and rating, so "why is MySQL due Oct 9?" has an answer (D-112).
    """
    started = time.perf_counter()
    if not isinstance(attempts, list) or not attempts:
        raise values.invalid(
            "attempts", "is empty or not a list", "send the problem's parts as a list of objects"
        )
    checked = [_attempt_values(raw, n) for n, raw in enumerate(attempts)]

    stamp = now()
    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        session = _session_for_attempts(conn, user_id, course_id, code, session_id, stamp)
        source, assisted = MODE_SOURCE[session.mode]
        ids, touched, reached = [], [], session.position
        for n, a in enumerate(checked):
            topic_id, paper, position = _attempt_topic(conn, user_id, course_id, code, a, n)
            attempt_id = new_id()
            conn.execute(
                insert(attempt_table).values(
                    id=attempt_id,
                    user_id=user_id,
                    session_id=session.id,
                    practice_item_id=a["practice_item_id"],
                    topic_id=topic_id,
                    source=source,
                    assisted=assisted,
                    correct=a["correct"],
                    score=a["score"],
                    root_cause=a["root_cause"],
                    fix_rule=a["fix_rule"],
                    correctness_voided=False,
                    created_at=stamp,
                )
            )
            ids.append(attempt_id)
            if topic_id not in touched:
                touched.append(topic_id)
            walked = session.subject_type == "past-exam" and paper == session.subject_id
            if session.mode == "exam-walkthrough" and walked and position is not None:
                reached = max(reached or 0, position)  # the question the walk has reached
        if reached != session.position:
            conn.execute(
                update(study_session)
                .where(study_session.c.id == session.id)
                .values(position=reached)
            )
        topics, logged = [], []
        for topic_id in touched:
            state = _store_state(conn, user_id, topic_id, tz)
            if state is None:
                topics.append({"topic_id": topic_id, "state": None})
                logged.append({"topic_id": topic_id, "state": None})
                continue
            shown = {k: state[k] for k in ("due_at", "reps", "lapses")}
            topics.append({"topic_id": topic_id, **shown})
            day = state["last_day"]
            logged.append(
                {"topic_id": topic_id, "day_score": day["score"], "rating": day["rating"], **shown}
            )

    # Report: after the commit, so only saved attempts are logged.
    log.info(
        json.dumps(
            {
                "event": "submit_attempt",
                "session_id": session.id,
                "mode": session.mode,
                "attempts": len(ids),
                "topics": logged,
                "ms": round((time.perf_counter() - started) * 1000),
            }
        )
    )
    return {"session_id": session.id, "attempt_ids": ids, "topics": topics}
