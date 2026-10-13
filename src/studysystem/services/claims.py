"""One paper's claims, judged against each other and against what the paper already holds
(2.1a; D-149, D-154). Pure: reads no database - the observation submit hands it the claims that
passed the schema and the claims already stored for the paper.
"""

from collections import Counter
from dataclasses import dataclass, field

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
