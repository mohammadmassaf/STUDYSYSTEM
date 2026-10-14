"""Routing a slot's claims into a profile (2.1b; D-14, D-150, D-152, D-157). Pure: no database.

The names are his live ones: I3302's course reads "A. Ghrayeb", its 2023-09 paper "Dr. Ali
Ghorayeb", its 2019-2021 papers "Dr. Mohamad Hamze"; I3305's papers are co-taught.
"""

import pytest

from studysystem.services.claims import route_claims, same_person

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
