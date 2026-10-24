"""Forward-chaining eval (2.8; D-12, D-14, D-22): may this course generate mock exams?

A gate, never a published number. Pure: reads no database.
"""

from collections import defaultdict
from collections.abc import Sequence

from studysystem.services.profiles import PaperItems, fill_marks, topic_weights

BAR = 3  # older papers a fold needs to count (D-22)
MIN_FOLDS = 2  # at-bar folds a pass needs, so 5+ papers (D-22)


def weighted_overlap(predicted: dict[str, float], actual: dict[str, float]) -> float:
    """How alike two topic mixes are, each summing to 100: 1.0 identical, 0.0 nothing shared."""
    score = 0
    for topic in actual.keys():
        act = actual.get(topic)
        pred = predicted.get(topic)
        if act and pred:
            score += min(act, pred)
    return score / 100


def forward_eval(papers: Sequence[PaperItems]) -> dict:
    """Predict each paper from the ones before it; `papers` oldest first.

    Returns {"verdict": "pass" | "fail" | "insufficient-evidence", "folds": [...]}, one fold per
    paper with BAR+ older papers: {"profile", "equal", "newest"} overlaps with the real mix, and
    "beats" - profile strictly above both baselines (a tie does not beat).
    """
    older = []
    folds = []
    for index, paper in enumerate(papers):
        if index < BAR:
            older.append(paper)
            continue
        real = topic_weights([paper])  # the answer key: what this paper actually asked
        # one pool fills every guess's unmarked items, so a paper read alone reads the same (D-212)
        filled = fill_marks(older)
        profile = topic_weights(filled[::-1])
        newest = topic_weights([filled[-1]])
        totals = defaultdict(float)
        for old in filled:
            for topic, share in topic_weights([old]).items():
                totals[topic] += share
        equal = {topic: total / len(filled) for topic, total in totals.items()}

        scores = {
            "profile": weighted_overlap(profile, real),
            "equal": weighted_overlap(equal, real),
            "newest": weighted_overlap(newest, real),
        }
        beats = scores["profile"] > scores["equal"] and scores["profile"] > scores["newest"]
        folds.append({**scores, "beats": beats})
        older.append(paper)

    if len(folds) < MIN_FOLDS:
        verdict = "insufficient-evidence"
    elif all(fold["beats"] for fold in folds):
        verdict = "pass"
    else:
        verdict = "fail"
    return {"verdict": verdict, "folds": folds}
