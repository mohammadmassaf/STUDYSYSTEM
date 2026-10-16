"""Claims, pure: read no database.

- One paper's claims, judged against each other and against what the paper already holds
  (2.1a; D-149, D-154) - the observation submit hands them the claims that passed the schema
  and the claims already stored for the paper.
- A slot's claims, routed by class into a profile version (2.1b; D-14, D-150, D-152, D-157) -
  `derive_profile` hands them the papers and the course's instructor.
- A profile's instructor-driven `provisional` flag and its reason (2.2; D-165, D-167) - from the
  instructor of each paper in its set.
- The paper that sets a profile's format, and its format `provisional` flag (2.3; D-168 - D-170) -
  from each paper's session and date, matched against the session the student sits next.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from studysystem.db.tables import CLAIM_KEYS

FORMAT_KEYS = frozenset(CLAIM_KEYS["format"])  # once per paper (D-154)
CLASS_OF = {key: cls for cls, keys in CLAIM_KEYS.items() for key in keys}


@dataclass
class Verdict:
    """The verdict on one submission's claims, each named by its place in the payload (from 1):
    `accepted` are written, `skipped` are already on the paper (never rewritten), `errors` go
    back as item errors."""

    accepted: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def duplicate_error(place: int, claim_key: str) -> dict:
    """The item error for a format key sent more than once in one payload."""
    return {
        "item_ordinal": place,
        "code": "duplicate_claim",
        "field": "claim_key",
        "message": f"{claim_key} is in this payload more than once - a paper has one; send only "
        "the value the paper prints",
    }


def _identity(claim: dict) -> tuple:
    """What makes two claims one (D-154): a format claim is its key, whatever its value; an
    instructor claim is its key and its text. The citation never counts."""
    if claim["claim_key"] in FORMAT_KEYS:
        return (claim["claim_key"],)
    else:  # an instructor key - the schema lets no other through
        return (claim["claim_key"], claim["claim_value"]["text"])


def judge_claims(claims: list[tuple[int, dict]], stored: list[dict]) -> Verdict:
    """Sort one submission's schema-valid claims (D-154).

    `claims`: (place, claim) pairs, place from 1 in the payload; a claim is {"claim_key",
    "claim_value"} and maybe "citation". `stored`: the claims already kept for this paper,
    {"claim_key", "claim_value"} each.
    """
    accepted = []
    skipped = []
    claim_keys = []
    errors = []

    for _, claim in claims:
        if claim["claim_key"] in FORMAT_KEYS:
            claim_keys.append(claim["claim_key"])
    count = Counter(claim_keys)

    on_paper = set()
    for claim in stored:
        on_paper.add(_identity(claim))
    seen = set()

    for place, claim in claims:
        identity = _identity(claim)
        if identity in on_paper:
            skipped.append(place)
        elif count[claim["claim_key"]] > 1:
            errors.append(duplicate_error(place, claim["claim_key"]))
        elif identity in seen:
            skipped.append(place)
        else:
            accepted.append(place)
            seen.add(identity)

    return Verdict(accepted=accepted, skipped=skipped, errors=errors)


# --- routing a slot's claims into a profile (2.1b) ---------------------------------------------

SAME_SURNAME = 0.8  # D-152: same-person pairs scored 0.93+, different people 0.50 or less
TITLES = frozenset({"dr", "m", "mr", "prof"})  # dropped before the surname is read (D-152)

# One paper as routing sees it: its `instructor` text (None when unknown) and its claims as
# (observation_id, claim_class) pairs.
PaperClaims = tuple[str | None, Sequence[tuple[str, str]]]


def _surnames(text: str) -> list[str]:
    """The surname of each name in an instructor text (D-152): split on commas, each name
    lowercased with dots as spaces and TITLES dropped, then its last word. A name that was only
    a title has none and is left out.

    "Dr. Mohamad Hamze, Dr. Abed EL Safadi" -> ["hamze", "safadi"]; "M.Dbouk" -> ["dbouk"];
    "Dr." -> [].
    """
    surnames = []
    for name in text.split(","):
        words = [word for word in name.lower().replace(".", " ").split() if word not in TITLES]
        if words:
            surnames.append(words[-1])
    return surnames


def same_person(paper_instructor: str | None, course_instructor: str | None) -> bool:
    """Whether a paper is by the course's instructor - who teaches now (D-150, D-152).

    Each name is tidied (lowercase, dots to spaces, TITLES dropped); a paper's text may name
    several people, split on commas, and so may the course's - any pair matching is enough (D-152).
    Two names match when their
    surnames (last word) score >= SAME_SURNAME by `SequenceMatcher(None, a, b).ratio()`. A
    missing name on either side never matches - unknown counts as not the same (D-150).

    "Dr. Ali Ghorayeb" vs "A. Ghrayeb" -> True (ghorayeb / ghrayeb 0.93)
    "Dr. Mohamad Hamze" vs "A. Ghrayeb" -> False (0.50)
    "Dr. Mohamad Hamze, Dr. Abed EL Safadi" vs "A. Safadi" -> True (I3305, the second name)
    None vs "A. Ghrayeb" -> False
    """
    if paper_instructor is None or course_instructor is None:
        return False
    paper_surnames = _surnames(paper_instructor)
    course_surnames = _surnames(course_instructor)
    for paper_name in paper_surnames:
        for course_name in course_surnames:
            if SequenceMatcher(None, paper_name, course_name).ratio() >= SAME_SURNAME:
                return True
    return False


def route_claims(papers: Sequence[PaperClaims], course_instructor: str | None) -> list[str]:
    """The observation ids a profile version keeps, papers given newest first (D-14).

    - format: only the newest paper that has a format claim keeps them, any session type
      until 2.3 (D-157).
    - instructor-driven: every paper by the course's instructor keeps them (`same_person`;
      D-150).
    - syllabus-driven: never routed here - weights derive from items (D-151).

    I3302 Final, after the 2021-09 observation task:
    [("Dr. Ali Ghorayeb", 3 format + 13 instructor), ("Dr. Mohamad Hamze", its claims), ...]
    with "A. Ghrayeb" -> the 16 ids of 2023-09; none of 2021-09's.
    """
    kept = []
    format_taken = False  # an earlier (newer) paper already gave format
    for instructor, claims in papers:
        has_format = any(claim_class == "format" for _, claim_class in claims)
        take_format = has_format and not format_taken
        by_course_instructor = same_person(instructor, course_instructor)
        for obs_id, claim_class in claims:
            if claim_class == "format" and take_format:
                kept.append(obs_id)
            elif claim_class == "instructor-driven" and by_course_instructor:
                kept.append(obs_id)
        if take_format:
            format_taken = True
    return kept


# --- a profile's provisional flags (2.2) ------------------------------------------------------

SAME_INSTRUCTOR_PAPERS = 2  # D-14: instructor-driven claims clear provisional at >= 2


def instructor_provisional(
    instructors: Sequence[str | None], course_instructor: str | None
) -> dict:
    """Whether a profile's instructor-driven claims are provisional, and why (D-14, D-165,
    D-167). `instructors`: the `instructor` text of each paper in the profile's set.

    Returns {"provisional": bool, "why": str}.

    I3302 Final: 1 Ghorayeb paper of 8, course "A. Ghrayeb" ->
    {"provisional": True, "why": "1 of 8 papers by A. Ghrayeb; 2 needed"}
    """
    if course_instructor is None:
        return {
            "provisional": True,
            "why": (
                f"the course names no instructor; {SAME_INSTRUCTOR_PAPERS} papers by them needed"
            ),
        }
    count = 0
    for instructor in instructors:
        if same_person(instructor, course_instructor):
            count += 1
    why = (
        f"{count} of {len(instructors)} papers by {course_instructor}; "
        f"{SAME_INSTRUCTOR_PAPERS} needed"
    )
    return {"provisional": count < SAME_INSTRUCTOR_PAPERS, "why": why}


# --- the paper that sets format (2.3) ---------------------------------------------------------

FORMAT_MAX_AGE = 5  # D-169: years, counted from today's year - 2021 is in during 2026, 2020 out

# One paper as the format rule sees it: its session type, its date ("YYYY-MM-DD"; a month-only
# paper is stored as the 1st, D-137) and whether it has a format claim.
FormatPaper = tuple[str, str, bool]


def _age(date: str, this_year: int) -> int:
    """A paper's age in whole years from its date's year: "2020-02-17" in 2026 -> 6."""
    return this_year - int(date[:4])


def format_paper(papers: Sequence[FormatPaper], session: str | None, this_year: int) -> int | None:
    """The position in `papers` (given newest first) of the paper whose format claims the profile
    keeps - None when there are no papers (D-14, D-168, D-169).

    `session` is the session the student sits next, read from the slot's `assessment` row; None
    (no sitting recorded) matches no paper, so the newest paper of any session sets format.
    The newest paper of that session sets format, observed or not; when it is more than
    FORMAT_MAX_AGE years old, the newest paper of the other session does if it is newer.
    Reads the date's year only.

    I3302 Final, "first", 2026 -> 0: the 2023-09 resit; its newest first, 2020-02, is 6 years old.
    I3306 Final, "first", 2026 -> 1: 2025-03, not the 2025-07 resit before it.
    """

    match = other = None  # the first position met of each - the newest, papers being sorted
    for pos, (session_type, _, _) in enumerate(papers):
        if session_type == session:
            if match is None:
                match = pos
        elif other is None:
            other = pos

    if match is None:  # no paper of that session: the other's newest, or None with no papers
        return other
    if _age(papers[match][1], this_year) <= FORMAT_MAX_AGE:
        return match
    if other is not None and other < match:  # a smaller position is a newer paper
        return other
    return match  # too old, but nothing newer to take


SAME_SESSION_PAPERS = 2  # D-14, D-170: format clears provisional at >= 2 of the matching session


def format_provisional(
    papers: Sequence[FormatPaper], session: str | None, this_year: int, chosen: int | None
) -> dict:
    """Whether a profile's format claims are provisional, and why (D-14, D-167, D-169, D-170).
    `papers`, `session` and `this_year` as `format_paper` takes them; `chosen` is its answer.

    Clears only when the matching session's paper set format and >= SAME_SESSION_PAPERS papers
    of that session, within FORMAT_MAX_AGE, have format claims. The reason names the paper that
    set format and the count, or why that paper falls short.

    I3302 Final, 2026 -> {"provisional": True, "why": "format from the 2023-09-13 second-session
    paper: the newest first-session paper, 2020-02-17, is more than 5 years old"}
    I3306 Final after one observation task -> {"provisional": True, "why": "format from
    2025-03-07; 1 of 3 first-session papers since 2021 read; 2 needed"}
    """
    if chosen is None:
        return {"provisional": True, "why": "no paper to take format from"}
    session_type, date, has_format = papers[chosen]
    unread = "" if has_format else "; not observed yet - run its observation task"

    if session is None:  # a slot with no exam sitting recorded: nothing to match
        why = f"format from {date}: the assessment records no session to match"
    elif session_type != session:  # the other session's paper is newer (D-169)
        newest = next((d for s, d, _ in papers if s == session), None)
        if newest is None:
            why = f"format from the {date} {session_type}-session paper: no {session}-session paper"
        else:
            why = (
                f"format from the {date} {session_type}-session paper: the newest "
                f"{session}-session paper, {newest}, is more than {FORMAT_MAX_AGE} years old"
            )
    elif _age(date, this_year) > FORMAT_MAX_AGE:
        why = f"format from {date}, more than {FORMAT_MAX_AGE} years old - no newer paper"
    elif not has_format:
        why = f"format from {date}"
    else:
        recent = [h for s, d, h in papers if s == session and _age(d, this_year) <= FORMAT_MAX_AGE]
        read = sum(recent)
        why = (
            f"format from {date}; {read} of {len(recent)} {session}-session papers since "
            f"{this_year - FORMAT_MAX_AGE} read; {SAME_SESSION_PAPERS} needed"
        )
        return {"provisional": read < SAME_SESSION_PAPERS, "why": why}
    return {"provisional": True, "why": why + unread}
