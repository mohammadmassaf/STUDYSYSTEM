"""The study loop (1.16). First the replay: a pure function, no database - a topic's attempts in,
its FSRS state out. The expected state is the package's own card, fed the reviews the case
should produce, so each test checks which reviews happen, not FSRS's arithmetic."""

import datetime

import pytest
from fsrs import Card, Rating

from studysystem.services.study_loop import SCHEDULER, AttemptRow, replay_topic, retrievability

BEIRUT = datetime.timezone(datetime.timedelta(hours=3))  # his offset in October
MYSQL_AVERAGE = 144 / 14  # MySQL's 14 items with marks: 10.3


def att(at, *, correct=None, score=None, marks=None, assisted=False, voided=False):
    return AttemptRow(
        created_at=at,
        assisted=assisted,
        correctness_voided=voided,
        correct=correct,
        score=score,
        marks=marks,
    )


def utc(text):
    return datetime.datetime.fromisoformat(text)


def expected(*reviews):
    """The card FSRS gives for these (rating, UTC time) reviews, in order."""
    card = Card()
    for rating, at in reviews:
        card, _ = SCHEDULER.review_card(card, rating, utc(at))
    return card


def assert_state(state, card, reps, lapses):
    assert state["stability"] == pytest.approx(card.stability)
    assert state["difficulty"] == pytest.approx(card.difficulty)
    assert state["due_at"] == card.due
    assert state["last_reviewed_at"] == card.last_review
    assert (state["reps"], state["lapses"]) == (reps, lapses)


# Tonight, Oct 6 (Beirut 19:00-19:30): the 2018-07-21 paper's index.php, four parts.
INDEX_PHP = [
    att("2026-10-06T16:00:00Z", correct=True, marks=5),
    att("2026-10-06T16:10:00Z", score=0.4, marks=5),
    att("2026-10-06T16:20:00Z", correct=True, marks=15),
    att("2026-10-06T16:30:00Z", correct=False, marks=25),
]
# Then 20:10: the 2023-09-13 paper's Question III, one 35-mark item.
Q_III = att("2026-10-06T17:10:00Z", correct=True, marks=35)
# Oct 9: the 2019-09-06 paper's Problem V, two parts transcribed with no marks.
PROBLEM_V = [
    att("2026-10-09T15:00:00Z", correct=True),
    att("2026-10-09T15:20:00Z", correct=False),
]


def test_one_problem_is_one_review_rated_on_its_weighted_score():
    # 22 / 50 = 0.44 -> Again, at the last part's time. Every Again is a lapse.
    state = replay_topic(INDEX_PHP, MYSQL_AVERAGE, BEIRUT)
    assert_state(state, expected((Rating.Again, "2026-10-06T16:30:00Z")), reps=1, lapses=1)


def test_a_later_problem_the_same_evening_joins_that_day():
    # 57 / 85 = 0.67 -> one Good, moved to 20:10 - not a second review.
    state = replay_topic([*INDEX_PHP, Q_III], MYSQL_AVERAGE, BEIRUT)
    assert_state(state, expected((Rating.Good, "2026-10-06T17:10:00Z")), reps=1, lapses=0)


def test_unknown_marks_on_every_part_weigh_the_same_and_half_is_again():
    # Oct 9: both parts borrow 10.3 -> 0.5, at the pass mark -> Again.
    state = replay_topic([*INDEX_PHP, Q_III, *PROBLEM_V], MYSQL_AVERAGE, BEIRUT)
    card = expected(
        (Rating.Good, "2026-10-06T17:10:00Z"),
        (Rating.Again, "2026-10-09T15:20:00Z"),
    )
    assert_state(state, card, reps=2, lapses=1)


def test_an_unknown_mark_borrows_the_topic_average():
    # 2018-01-23: pos 1 (marks unknown) ok, pos 3 (7 marks) wrong -> 10.3 / 17.3 = 0.60 -> Good.
    attempts = [
        att("2026-10-06T16:00:00Z", correct=True),
        att("2026-10-06T16:30:00Z", correct=False, marks=7),
    ]
    state = replay_topic(attempts, MYSQL_AVERAGE, BEIRUT)
    assert_state(state, expected((Rating.Good, "2026-10-06T16:30:00Z")), reps=1, lapses=0)


def test_a_topic_with_no_known_marks_weighs_every_part_the_same():
    # 2 of 3 ok -> 0.67 -> Good.
    attempts = [
        att("2026-10-06T16:00:00Z", correct=True),
        att("2026-10-06T16:10:00Z", correct=True),
        att("2026-10-06T16:20:00Z", correct=False),
    ]
    state = replay_topic(attempts, None, BEIRUT)
    assert_state(state, expected((Rating.Good, "2026-10-06T16:20:00Z")), reps=1, lapses=0)


def test_a_day_of_zero_mark_items_weighs_every_part_the_same():
    # marks 0 leaves nothing to share: 2 of 3 ok -> 0.67 -> Good, not a divide-by-zero.
    attempts = [
        att("2026-10-06T16:00:00Z", correct=True, marks=0),
        att("2026-10-06T16:10:00Z", correct=True, marks=0),
        att("2026-10-06T16:20:00Z", correct=False, marks=0),
    ]
    state = replay_topic(attempts, MYSQL_AVERAGE, BEIRUT)
    assert_state(state, expected((Rating.Good, "2026-10-06T16:20:00Z")), reps=1, lapses=0)


def test_a_score_alone_is_used_as_given():
    state = replay_topic([att("2026-10-06T16:00:00Z", score=0.8, marks=5)], MYSQL_AVERAGE, BEIRUT)
    assert_state(state, expected((Rating.Good, "2026-10-06T16:00:00Z")), reps=1, lapses=0)


@pytest.mark.parametrize(
    ("correct", "score", "rating"),
    [(True, 0.4, Rating.Again), (False, 0.8, Rating.Good)],
    ids=["ok-but-0.4", "wrong-but-0.8"],
)
def test_a_score_wins_over_correct(correct, score, rating):
    attempt = att("2026-10-06T16:00:00Z", correct=correct, score=score, marks=5)
    state = replay_topic([attempt], MYSQL_AVERAGE, BEIRUT)
    lapses = 1 if rating == Rating.Again else 0
    assert_state(state, expected((rating, "2026-10-06T16:00:00Z")), reps=1, lapses=lapses)


def test_assisted_attempts_do_not_count():
    # A walkthrough of index.php, then one part solved alone: only that part counts.
    walked = [att(a.created_at, correct=False, marks=a.marks, assisted=True) for a in INDEX_PHP]
    alone = att("2026-10-06T17:00:00Z", correct=True, marks=5)
    state = replay_topic([*walked, alone], MYSQL_AVERAGE, BEIRUT)
    assert_state(state, expected((Rating.Good, "2026-10-06T17:00:00Z")), reps=1, lapses=0)


def test_a_voided_attempt_is_replayed_away():
    # Q III's correctness voided (wrong key): the evening falls back to index.php alone.
    voided = att(Q_III.created_at, correct=True, marks=35, voided=True)
    state = replay_topic([*INDEX_PHP, voided], MYSQL_AVERAGE, BEIRUT)
    assert_state(state, expected((Rating.Again, "2026-10-06T16:30:00Z")), reps=1, lapses=1)


@pytest.mark.parametrize(
    "attempts",
    [
        [],
        [att(a.created_at, correct=True, marks=a.marks, assisted=True) for a in INDEX_PHP],
        [att(a.created_at, correct=True, marks=a.marks, voided=True) for a in INDEX_PHP],
    ],
    ids=["none", "all-assisted", "all-voided"],
)
def test_nothing_counts_gives_no_state(attempts):
    assert replay_topic(attempts, MYSQL_AVERAGE, BEIRUT) is None


def test_the_day_is_the_local_date():
    # 23:30 Oct 6 Beirut joins the evening; 00:30 is Oct 7 - its own review. Both are Oct 6 in UTC.
    attempts = [
        att("2026-10-06T16:30:00Z", correct=True, marks=5),
        att("2026-10-06T20:30:00Z", correct=True, marks=5),
        att("2026-10-06T21:30:00Z", correct=False, marks=5),
    ]
    state = replay_topic(attempts, MYSQL_AVERAGE, BEIRUT)
    card = expected(
        (Rating.Good, "2026-10-06T20:30:00Z"),
        (Rating.Again, "2026-10-06T21:30:00Z"),
    )
    assert_state(state, card, reps=2, lapses=1)


def test_the_order_attempts_arrive_in_does_not_matter():
    attempts = [*INDEX_PHP, Q_III, *PROBLEM_V]
    assert replay_topic(attempts[::-1], MYSQL_AVERAGE, BEIRUT) == replay_topic(
        attempts, MYSQL_AVERAGE, BEIRUT
    )


def test_every_again_is_a_lapse():
    attempts = [
        att("2026-10-06T16:00:00Z", correct=False, marks=5),
        att("2026-10-09T16:00:00Z", correct=False, marks=5),
    ]
    state = replay_topic(attempts, MYSQL_AVERAGE, BEIRUT)
    card = expected(
        (Rating.Again, "2026-10-06T16:00:00Z"),
        (Rating.Again, "2026-10-09T16:00:00Z"),
    )
    assert_state(state, card, reps=2, lapses=2)


# --- retrievability: weakness = 1 - this, in the plan (D-82) ---------------------------------

ONE_GOOD = expected((Rating.Good, "2026-10-06T17:10:00Z"))  # S 2.31, reviewed 20:10 Beirut


def recall_after(days):
    """The package's own recall `days` whole days after ONE_GOOD's review."""
    moment = ONE_GOOD.last_review + datetime.timedelta(days=days)
    return SCHEDULER.get_card_retrievability(ONE_GOOD, moment)


@pytest.mark.parametrize(
    ("today", "days"),
    [
        (datetime.date(2026, 10, 6), 0),  # the evening itself: full recall
        (datetime.date(2026, 10, 7), 1),  # next day, even before 20:10
        (datetime.date(2026, 10, 16), 10),
        (datetime.date(2026, 10, 5), 0),  # a today before the review counts as 0 days
    ],
    ids=["same-day", "next-day", "ten-days", "before"],
)
def test_recall_counts_whole_local_days_since_the_review(today, days):
    recall = retrievability(ONE_GOOD.stability, "2026-10-06T17:10:00Z", today, BEIRUT)
    assert recall == pytest.approx(recall_after(days))


def test_recall_dates_the_review_in_the_local_zone():
    # 00:30 Oct 7 in Beirut is still Oct 6 in UTC: on Oct 7 it is 0 days old, not 1.
    recall = retrievability(2.31, "2026-10-06T21:30:00Z", datetime.date(2026, 10, 7), BEIRUT)
    assert recall == pytest.approx(1.0)
