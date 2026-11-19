"""Discrimination trials in walkthroughs (4.13; D-24, D-312 - D-318).

Off by default (D-312): a student turns them on with `set_trials`. With trials on, a walkthrough
of a past paper hides one generated item among the paper's questions (`walkthrough_questions`),
every answer in that walkthrough carries the guess "real or generated?", and `record_trial`
writes one `discrimination_trial` row per item. Every walkthrough answer on an item is then
revealed (`reveal`), trials on or off, so a hidden `unverified` key never stays unmarked (D-316).
"""

import random

from sqlalchemy import Connection, Engine, exists, select, update

from studysystem.db.tables import attempt, discrimination_trial, past_exam, practice_item, user
from studysystem.errors import StudyError
from studysystem.services import values
from studysystem.services.ids import new_id
from studysystem.services.units import write_unit

TELLS = ("too-easy", "phrasing", "topic-scope", "formatting", "no-tell")
# The states a question is shown in: `topics.LISTED`, repeated here because `topics` imports
# `study_loop`, which imports this module.
SHOWN = ("live", "needs-review")


# --- the switch (D-313, D-317) -------------------------------------------------------------


def set_trials(engine: Engine, user_id: str, on: object) -> dict:
    """Turn the student's trials on or off. The tool and the dashboard route both call this.
    Returns {"trials_on"}."""
    on = values.flag("on", on)
    with write_unit(engine) as conn:
        done = conn.execute(update(user).where(user.c.id == user_id).values(trials_on=int(on)))
        if done.rowcount != 1:
            raise StudyError(
                code="not_found",
                message="no user with this id",
                fix="run: study migrate",
                field_errors=[{"field": "user_id", "problem": "no such user"}],
            )
    return {"trials_on": on}


def trials_on(conn: Connection, user_id: str) -> bool:
    """The switch as it is now. Read at submit, not at the session's start (D-316)."""
    flag = conn.execute(select(user.c.trials_on).where(user.c.id == user_id)).scalar_one()
    return flag == 1


# --- hiding the item (D-314, D-318) --------------------------------------------------------


def draw(candidates: list[str], real_count: int, seed: str) -> tuple[str, int] | None:
    """Which candidate to hide, and its place in the walk (pure; D-318).

    The place is a number from 2 to real_count + 1: never 1, so a real question always comes
    before it, and at most one past the last real question. The same seed gives the same answer,
    so a session id always gives the same walk. No candidate, or no real question -> None.

    ["a", "b", "c"], 4 real questions -> e.g. ("b", 3): b is shown as question 3 of 5.
    """
    if not candidates or real_count == 0:
        return None
    rng = random.Random(seed)
    return rng.choice(candidates), rng.randint(2, real_count + 1)


def walkthrough_questions(
    conn: Connection, user_id: str, paper_id: str, seed: str, hide: bool
) -> list[dict]:
    """The paper's questions in order, each as {"practice_item_id", "number", "question"}; with
    `hide`, one generated item sits among them in the same shape (D-314, D-316).

    The shape carries nothing a generated item lacks - no paper date, no marks, no scan page -
    since any one of them would tell it apart.

    A candidate is a generated item on one of the paper's topics, live or needs-review, never
    attempted (attempted once, it was revealed once: no longer blind).
    """
    real = conn.execute(
        select(practice_item.c.id, practice_item.c.question, practice_item.c.topic_id)
        .where(
            practice_item.c.past_exam_id == paper_id,
            practice_item.c.user_id == user_id,
            practice_item.c.state.in_(SHOWN),
        )
        .order_by(practice_item.c.position)
    ).all()
    shown = [(row.id, row.question) for row in real]

    if hide:
        paper_topics = {row.topic_id for row in real}
        attempted = exists().where(attempt.c.practice_item_id == practice_item.c.id)
        candidates = conn.execute(
            select(practice_item.c.id, practice_item.c.question)
            .where(
                practice_item.c.user_id == user_id,
                practice_item.c.origin == "generated",
                practice_item.c.state.in_(SHOWN),
                practice_item.c.topic_id.in_(paper_topics),
                ~attempted,
            )
            .order_by(practice_item.c.id)  # a fixed order, so the seed alone decides the draw
        ).all()
        picked = draw([row.id for row in candidates], len(shown), seed)
        if picked is not None:
            item_id, place = picked
            question = next(row.question for row in candidates if row.id == item_id)
            shown.insert(place - 1, (item_id, question))

    return [
        {"practice_item_id": item_id, "number": n, "question": question}
        for n, (item_id, question) in enumerate(shown, start=1)
    ]


# --- recording the trial and the reveal (D-315, D-316) -------------------------------------


def record_trial(
    conn: Connection,
    user_id: str,
    session_id: str,
    item_id: str,
    topic_id: str,
    guess: dict,
    where: str,
    stamp: str,
) -> None:
    """Write the item's trial in this walkthrough, inside the attempts' write unit. Called only
    when trials are on and the attempt names an item.

    - a trial already stands for this item in this walk -> nothing: the first guess was blind,
      a later one comes after the reveal (and the unique key would refuse a second row)
    - no guess -> refused, so the whole unit rolls back, the attempt with it
    - else one row; `was_generated` is the item's origin as it is now, when shown
    """
    done = conn.execute(
        select(discrimination_trial.c.id).where(
            discrimination_trial.c.session_id == session_id,
            discrimination_trial.c.practice_item_id == item_id,
        )
    ).first()
    if done is not None:
        return
    if guess["flagged"] is None:
        raise StudyError(
            code="guess_missing",
            message=f"{where}: trials are on, and this answer has no guess",
            fix="ask the student 'real or generated?' and send flagged: true for generated, "
            "false for real - before showing where the item came from",
            field_errors=[{"field": f"{where}.flagged", "problem": "is missing"}],
        )
    origin = conn.execute(
        select(practice_item.c.origin).where(practice_item.c.id == item_id)
    ).scalar_one()
    conn.execute(
        discrimination_trial.insert().values(
            id=new_id(),
            user_id=user_id,
            session_id=session_id,
            practice_item_id=item_id,
            topic_id=topic_id,
            was_generated=int(origin == "generated"),
            flagged=int(guess["flagged"]),
            tell=guess["tell"],
            created_at=stamp,
        )
    )


def reveal(conn: Connection, item_ids: list[str]) -> list[dict]:
    """Where each answered item came from, in the order given: {"practice_item_id", "origin",
    "answer_provenance", "paper_date"} - paper_date None for an item from no paper."""
    rows = conn.execute(
        select(
            practice_item.c.id,
            practice_item.c.origin,
            practice_item.c.answer_provenance,
            past_exam.c.session_date,
        )
        # Outer: a generated item has no paper, and must still be revealed - with no paper date.
        .join_from(
            practice_item,
            past_exam,
            practice_item.c.past_exam_id == past_exam.c.id,
            isouter=True,
        )
        .where(practice_item.c.id.in_(item_ids))
    ).all()
    by_id = {row.id: row for row in rows}
    return [
        {
            "practice_item_id": item_id,
            "origin": by_id[item_id].origin,
            "answer_provenance": by_id[item_id].answer_provenance,
            "paper_date": by_id[item_id].session_date,
        }
        for item_id in dict.fromkeys(item_ids)  # an item answered twice is revealed once
    ]
