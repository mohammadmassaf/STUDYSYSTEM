"""Forward-chaining eval (2.8; D-12, D-14, D-22): may this course generate mock exams?

Papers go in oldest first. Each paper is a topic -> item-count dict, so a fixture reads as a mix.
The verdict is `pass`, `fail` or `insufficient-evidence`; a fold counts only with 3+ older papers.
"""

import pytest

from studysystem.services.forward_eval import forward_eval, weighted_overlap


def paper(counts):
    """{"a": 3, "b": 1} -> three items on a, one on b, no marks printed."""
    return [(topic, None) for topic, n in counts.items() for _ in range(n)]


def papers(*mixes):
    """Mixes as tuples over topics a, b, c, d -> papers, oldest first."""
    return [paper(dict(zip("abcd", mix, strict=True))) for mix in mixes]


def test_overlap_of_identical_mixes_is_one():
    mix = {"a": 60.0, "b": 40.0}
    assert weighted_overlap(mix, mix) == pytest.approx(1.0)


def test_overlap_of_disjoint_mixes_is_zero():
    assert weighted_overlap({"a": 100.0}, {"b": 100.0}) == pytest.approx(0.0)


def test_overlap_counts_a_topic_missing_on_one_side_as_zero():
    # a: min(70, 50) = 50, b: min(30, 0) = 0, c: min(0, 50) = 0
    assert weighted_overlap({"a": 70.0, "b": 30.0}, {"a": 50.0, "c": 50.0}) == pytest.approx(0.5)


def test_three_papers_are_insufficient_evidence():
    """3 papers, so no fold has 3 older papers."""
    result = forward_eval(papers((4, 3, 2, 1), (3, 3, 2, 2), (4, 2, 2, 2)))
    assert result["verdict"] == "insufficient-evidence"
    assert result["folds"] == []


def test_four_papers_are_insufficient_evidence():
    """One at-bar fold; pass needs two."""
    result = forward_eval(papers((4, 3, 2, 1), (3, 3, 2, 2), (4, 2, 2, 2), (3, 3, 3, 1)))
    assert result["verdict"] == "insufficient-evidence"
    assert len(result["folds"]) == 1


def test_five_papers_where_profile_beats_both_baselines_pass():
    # The early papers lean on a, then the course shifts to c and d (fixture found by search).
    result = forward_eval(
        papers((5, 4, 2, 0), (4, 2, 2, 0), (1, 3, 4, 3), (2, 3, 3, 1), (3, 2, 3, 2))
    )
    assert len(result["folds"]) == 2
    assert result["verdict"] == "pass"


def test_six_papers_where_profile_beats_both_baselines_in_every_fold_pass():
    result = forward_eval(
        papers(
            (5, 4, 2, 0),
            (4, 2, 2, 0),
            (1, 3, 4, 3),
            (2, 3, 3, 1),
            (3, 2, 3, 2),
            (2, 3, 2, 3),
        )
    )
    assert len(result["folds"]) == 3
    assert all(fold["beats"] for fold in result["folds"])
    assert result["verdict"] == "pass"


def test_six_papers_where_the_newest_paper_alone_wins_a_fold_fail():
    """A steady drift: the newest older paper is the best predictor, so the profile loses."""
    result = forward_eval(
        papers((8, 1, 1, 0), (6, 2, 1, 1), (4, 3, 2, 1), (3, 3, 2, 2), (2, 2, 3, 3), (1, 1, 4, 4))
    )
    assert len(result["folds"]) == 3
    assert not any(fold["beats"] for fold in result["folds"])
    assert result["verdict"] == "fail"


def test_a_tie_with_a_baseline_does_not_beat_it():
    """Six identical papers: profile, equal weights and newest-alone all score 1.0."""
    result = forward_eval(papers(*[(4, 3, 2, 1)] * 6))
    assert [fold["beats"] for fold in result["folds"]] == [False, False, False]
    assert result["verdict"] == "fail"


def test_a_topic_only_the_newest_paper_has_does_not_crash():
    result = forward_eval(
        [
            *papers((4, 3, 2, 1), (3, 3, 2, 2), (4, 2, 2, 2), (3, 3, 3, 1)),
            paper({"a": 2, "z": 3}),
        ]
    )
    assert len(result["folds"]) == 2
    assert result["verdict"] in ("pass", "fail")


def test_a_paper_read_alone_fills_its_unmarked_items_from_the_older_papers():
    """P3 prints no mark for a. Read alone it would split by count (50/50); filled from P1-P2's
    a = 10 it reads 25/75 like the others, so both baselines match the target exactly."""
    marked = [("a", 10), ("b", 30)]
    result = forward_eval([marked, marked, [("a", None), ("b", 30)], marked])
    fold = result["folds"][0]
    assert fold["newest"] == pytest.approx(1.0)
    assert fold["equal"] == pytest.approx(1.0)
