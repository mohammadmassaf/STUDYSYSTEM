"""Routing a slot's claims into a profile (2.1b; D-14, D-150, D-152, D-157), and the instructor
bar on its `provisional` flag (2.2; D-165), and the paper that sets format with its flag (2.3;
D-168 - D-170).
Pure: no database.

The names are his live ones: I3302's course reads "A. Ghrayeb", its 2023-09 paper "Dr. Ali
Ghorayeb", its 2019-2021 papers "Dr. Mohamad Hamze"; I3305's papers are co-taught.
"""

import pytest

from studysystem.services.claims import (
    format_paper,
    format_provisional,
    instructor_provisional,
    route_claims,
    same_person,
)

SAME_PERSON = [
    ("Dr. Ali Ghorayeb", "A. Ghrayeb", True),  # spelling drift, 0.93
    ("Dr. Mohamad Hamze", "A. Ghrayeb", False),  # another lecturer, 0.50
    ("Dr. Mohamad Hamze, Dr. Abed EL Safadi", "A. Safadi", True),  # co-taught, the second name
    ("DBOUK", "M. Dbouk", True),  # case
    ("M.Dbouk", "M. Dbouk", True),  # a dot with no space after it
    (None, "A. Ghrayeb", False),  # the paper names no one (tier unknown)
    ("Dr. Ali Ghorayeb", None, False),  # the course names no one
    ("Dr.", "A. Ghrayeb", False),  # a title only: no surname, no crash
]


@pytest.mark.parametrize(("paper", "course", "expected"), SAME_PERSON)
def test_same_person(paper, course, expected):
    assert same_person(paper, course) is expected


# --- route_claims ---------------------------------------------------------------------------

FMT, INS = "format", "instructor-driven"
GHORAYEB, HAMZE = "Dr. Ali Ghorayeb", "Dr. Mohamad Hamze"
NOW = "A. Ghrayeb"  # I3302's course.instructor


def test_route_keeps_the_newest_format_and_the_instructors_own_claims():
    """The main rule: format from the newest paper only, instructor claims by who teaches now."""
    papers = [
        (GHORAYEB, [("obs1", FMT), ("obs2", INS), ("obs3", INS)]),  # 2023-09
        (HAMZE, [("obs4", FMT), ("obs5", INS)]),  # 2021-09
        (None, [("obs6", FMT)]),  # 2018, no name
    ]
    assert sorted(route_claims(papers, NOW)) == ["obs1", "obs2", "obs3"]


def test_route_keeps_every_format_claim_of_that_paper():
    """90 min, 4 questions, no MCQ: one paper's three format claims all stay."""
    papers = [(GHORAYEB, [("dur", FMT), ("count", FMT), ("mcq", FMT)]), (HAMZE, [("old", FMT)])]
    assert sorted(route_claims(papers, NOW)) == ["count", "dur", "mcq"]


def test_route_walks_on_for_format_when_the_newest_paper_has_none():
    """A paper that prints no duration gives traps only; format comes from the next one (D-157)."""
    papers = [(GHORAYEB, [("trap", INS)]), (HAMZE, [("dur", FMT)]), (None, [("older", FMT)])]
    assert sorted(route_claims(papers, NOW)) == ["dur", "trap"]


def test_route_newest_paper_does_not_make_its_instructor_claims_trusted():
    """Hamze's paper is newest: its format is kept, its trap is not (D-150)."""
    papers = [(HAMZE, [("dur", FMT), ("hamze_trap", INS)]), (GHORAYEB, [("ghorayeb_trap", INS)])]
    assert sorted(route_claims(papers, NOW)) == ["dur", "ghorayeb_trap"]


def test_route_keeps_instructor_claims_from_every_paper_by_that_instructor():
    """Two Ghorayeb papers both count - 2.2's threshold needs >= 2 of them (D-14)."""
    papers = [(GHORAYEB, [("a", INS)]), (HAMZE, [("h", INS)]), ("Dr. A. Ghorayeb", [("b", INS)])]
    assert sorted(route_claims(papers, NOW)) == ["a", "b"]


def test_route_excludes_instructor_claims_when_the_instructor_changes():
    """2.1's Done when: the same papers, the course's instructor switched to Hamze. His claims
    come in, Ghorayeb's go out; format, kept across a change, does not move (D-14)."""
    papers = [
        (GHORAYEB, [("dur", FMT), ("ghorayeb_trap", INS)]),
        (HAMZE, [("old_dur", FMT), ("hamze_trap", INS)]),
    ]
    assert sorted(route_claims(papers, NOW)) == ["dur", "ghorayeb_trap"]
    assert sorted(route_claims(papers, "M. Hamze")) == ["dur", "hamze_trap"]


def test_route_with_no_format_claim_keeps_instructor_claims_only():
    papers = [(GHORAYEB, [("trap", INS)]), (HAMZE, [("other", INS)])]
    assert route_claims(papers, NOW) == ["trap"]


def test_route_with_an_unknown_course_instructor_keeps_format_only():
    """No one declared as teaching now: no paper is by them (D-150)."""
    papers = [(GHORAYEB, [("dur", FMT), ("trap", INS)])]
    assert route_claims(papers, None) == ["dur"]


def test_route_of_no_papers_keeps_nothing():
    assert route_claims([], NOW) == []


# --- instructor_provisional (2.2; D-14, D-165, D-167) -----------------------------------------


def test_instructor_change_leaves_one_paper_by_who_teaches_now():
    """(a) I3302 Final: Hamze set 2019-2021, Ghorayeb 2023-09, two papers name no one."""
    instructors = [GHORAYEB] + [HAMZE] * 5 + [None] * 2
    assert instructor_provisional(instructors, NOW) == {
        "provisional": True,
        "why": "1 of 8 papers by A. Ghrayeb; 2 needed",
    }


def test_many_papers_by_the_same_instructor_clear_it():
    """(b) I3306 Final: Dbouk on 7 of 8, one paper names no one."""
    instructors = ["M. DBOUK"] * 7 + [None]
    assert instructor_provisional(instructors, "M. Dbouk") == {
        "provisional": False,
        "why": "7 of 8 papers by M. Dbouk; 2 needed",
    }


def test_exactly_two_papers_clear_it():
    """(c) the bar itself: two Ghorayeb papers, spelled two ways."""
    instructors = [GHORAYEB, HAMZE, "Dr. A. Ghorayeb"]
    assert instructor_provisional(instructors, NOW) == {
        "provisional": False,
        "why": "2 of 3 papers by A. Ghrayeb; 2 needed",
    }


def test_a_course_naming_no_instructor_is_provisional():
    """(d) like DHR300: no one declared as teaching now, so no paper is by them (D-150)."""
    assert instructor_provisional([GHORAYEB, GHORAYEB], None) == {
        "provisional": True,
        "why": "the course names no instructor; 2 papers by them needed",
    }


def test_papers_naming_no_one_never_count():
    """(e) I3301 Final: five papers, none names an instructor."""
    assert instructor_provisional([None] * 5, "K. Beydoun") == {
        "provisional": True,
        "why": "0 of 5 papers by K. Beydoun; 2 needed",
    }


def test_a_co_taught_paper_counts_for_either_name():
    """(f) I3305: 2019-09 names Hamze and Safadi - it is a Safadi paper too (D-152)."""
    instructors = ["Dr. Mohamad Hamze, Dr. Abed EL Safadi", "Dr. Abed El Safadi"]
    assert instructor_provisional(instructors, "A. Safadi") == {
        "provisional": False,
        "why": "2 of 2 papers by A. Safadi; 2 needed",
    }


# --- format_paper (2.3; D-14, D-168, D-169) ---------------------------------------------------

YEAR = 2026  # today's year when these were written; 5-year cap -> 2021 in, 2020 out


def test_a_first_session_past_the_cap_gives_way_to_a_newer_resit():
    """(1) I3302 Final: newest first is 2020-02 (6 years), the 2023-09 resit is newer."""
    papers = [
        ("second", "2023-09-13", True),
        ("second", "2021-09-20", True),
        ("second", "2020-09-14", False),
        ("first", "2020-02-17", False),
        ("second", "2019-09-06", False),
        ("first", "2019-01-24", False),
        ("second", "2018-07-21", False),
        ("first", "2018-01-23", False),
    ]
    assert format_paper(papers, "first", YEAR) == 0


def test_the_matching_session_beats_a_newer_resit():
    """(2) I3306 Final: the 2025-07 resit is newest, the 2025-03 first session sets format."""
    papers = [
        ("second", "2025-07-24", False),
        ("first", "2025-03-07", True),
        ("first", "2022-04-13", False),
    ]
    assert format_paper(papers, "first", YEAR) == 1


def test_an_unobserved_newest_match_is_still_the_one():
    """(3) the newest first is not observed: it is picked anyway, never an older observed one -
    its format is empty until its observation task runs."""
    papers = [("first", "2025-03-07", False), ("first", "2022-04-13", True)]
    assert format_paper(papers, "first", YEAR) == 0


def test_no_paper_of_the_matching_session_takes_the_newest_other():
    """(4) only resits in the set."""
    papers = [("second", "2024-07-18", True), ("second", "2023-09-13", True)]
    assert format_paper(papers, "first", YEAR) == 0


def test_an_old_match_stays_when_the_other_session_is_older_still():
    """(5) both past the cap: the newer of the two, here the first session."""
    papers = [("first", "2019-01-24", True), ("second", "2018-07-21", True)]
    assert format_paper(papers, "first", YEAR) == 0


@pytest.mark.parametrize(("date", "expected"), [("2021-01-10", 1), ("2020-12-30", 0)])
def test_the_cap_counts_whole_years(date, expected):
    """(6) in 2026 a 2021 first session is 5 years old - in, it beats the newer resit; a 2020 one
    is 6 - out, the resit sets format. The month never counts."""
    papers = [("second", "2023-06-01", True), ("first", date, True)]
    assert format_paper(papers, "first", YEAR) == expected


def test_no_papers_set_no_format():
    assert format_paper([], "first", YEAR) is None


def test_the_matching_session_is_an_input():
    """A student sitting the resit matches `second`: the same papers, the other answer."""
    papers = [("first", "2025-03-07", True), ("second", "2024-07-18", True)]
    assert format_paper(papers, "second", YEAR) == 1


# --- format_provisional (2.3; D-14, D-167, D-169, D-170) --------------------------------------

I3302 = [  # his live I3302 Final, newest first: two resits observed, no first session
    ("second", "2023-09-13", True),
    ("second", "2021-09-20", True),
    ("second", "2020-09-14", False),
    ("first", "2020-02-17", False),
    ("second", "2019-09-06", False),
    ("first", "2019-01-24", False),
]
I3306 = [  # I3306 Final after the observation task on 2025-03, newest first
    ("second", "2025-07-24", False),
    ("first", "2025-03-07", True),
    ("first", "2022-04-13", False),
    ("first", "2021-08-06", False),
    ("first", "2019-01-25", False),
]


def test_a_resit_setting_format_is_provisional_and_says_why():
    """(7) I3302: the fallback (D-169) never clears the bar, whatever the count."""
    assert format_provisional(I3302, "first", YEAR, 0) == {
        "provisional": True,
        "why": "format from the 2023-09-13 second-session paper: the newest first-session paper, "
        "2020-02-17, is more than 5 years old",
    }


def test_one_read_paper_of_the_session_is_provisional():
    """(8) I3306: 2025-03 read; 2022-04 and 2021-08 in the cap but not read; 2019 outside it."""
    assert format_provisional(I3306, "first", YEAR, 1) == {
        "provisional": True,
        "why": "format from 2025-03-07; 1 of 3 first-session papers since 2021 read; 2 needed",
    }


def test_two_read_papers_of_the_session_clear_it():
    """(9) the bar itself: 2022-04 read too."""
    papers = list(I3306)
    papers[2] = ("first", "2022-04-13", True)
    assert format_provisional(papers, "first", YEAR, 1) == {
        "provisional": False,
        "why": "format from 2025-03-07; 2 of 3 first-session papers since 2021 read; 2 needed",
    }


def test_a_read_paper_past_the_cap_does_not_count():
    """(10) 2019-01 read: more than 5 years old, so still one."""
    papers = list(I3306)
    papers[4] = ("first", "2019-01-25", True)
    assert format_provisional(papers, "first", YEAR, 1)["provisional"] is True
    assert "1 of 3" in format_provisional(papers, "first", YEAR, 1)["why"]


def test_a_picked_paper_never_observed_names_the_task_to_run():
    papers = [("first", "2025-03-07", False), ("first", "2022-04-13", True)]
    assert format_provisional(papers, "first", YEAR, 0) == {
        "provisional": True,
        "why": "format from 2025-03-07; not observed yet - run its observation task",
    }


def test_an_old_match_with_nothing_newer_says_so():
    papers = [("first", "2019-01-24", True), ("second", "2018-07-21", True)]
    assert format_provisional(papers, "first", YEAR, 0) == {
        "provisional": True,
        "why": "format from 2019-01-24, more than 5 years old - no newer paper",
    }


def test_no_paper_of_the_session_says_so():
    papers = [("second", "2024-07-18", False)]
    assert format_provisional(papers, "first", YEAR, 0) == {
        "provisional": True,
        "why": "format from the 2024-07-18 second-session paper: no first-session paper; "
        "not observed yet - run its observation task",
    }


def test_no_session_to_match_says_so():
    papers = [("first", "2025-03-07", True)]
    assert format_provisional(papers, None, YEAR, 0) == {
        "provisional": True,
        "why": "format from 2025-03-07: the assessment records no session to match",
    }


def test_no_paper_at_all():
    assert format_provisional([], "first", YEAR, None) == {
        "provisional": True,
        "why": "no paper to take format from",
    }
