"""The `unconfirmed` flag (4.5; D-16 c, D-266, D-267): the mis-tag lazy confirmation cannot reach.

During term the study lane shows only topics tagged to a taught chapter (D-118). A chapter that
is taught but tagged to no topic hides whatever it teaches from the plan until exam prep. The
flag pairs the two halves of that evidence, per course: the taught chapters with no tag, and the
topics nothing has reached. Computed on every read - nothing is stored, and
`coverage.confirmed_at` stays NULL (D-266).

I3304's shape -> Chapter7 LAN Redundancy (noted, no tag) beside the topics no chapter reaches:
NAT, OSPF, VLANs, ...
"""

from sqlalchemy import Connection, Select, exists, select

from studysystem.db.tables import attempt, coverage, hours_entry, material, topic
from studysystem.services.plan import _taught_materials, chapter_name
from studysystem.services.topics import TAGGABLE


def untagged_taught_chapters(user_id: str) -> Select:
    """The user's taught chapters that no `coverage` edge ties to a live topic (D-267), as a
    query of (id, course_id, filename).

    Taught is D-120's rule, already built: `_taught_materials(user_id)` gives the ids of chapters
    with a live note or a study session. Live topic = status in `TAGGABLE`.

    I3304's Chapter7 LAN Redundancy: noted, no edge          -> listed
    I3304's Chapter3-HTTP-1: noted, -> HTTP & web performance -> not listed
    a chapter whose only edge points to a superseded topic   -> listed
    a chapter fed but never noted or studied                 -> not listed (not taught)
    another user's chapter                                   -> not listed

    Same pattern as plan.py:437 ("no such row exists") and plan.py:440-446 (an edge to a taught
    chapter), turned around: this asks it of the chapter, not of the topic.
    """

    live_edge = exists().where(
        coverage.c.material_id == material.c.id,
        coverage.c.topic_id == topic.c.id,
        topic.c.status.in_(TAGGABLE),
    )
    return select(material.c.id, material.c.course_id, material.c.filename).where(
        material.c.user_id == user_id, material.c.id.in_(_taught_materials(user_id)), ~live_edge
    )


def unreached_topics(user_id: str) -> Select:
    """The user's `active` topics nothing has reached (D-266), as a query of (id, course_id,
    name). Reached = hours logged on the topic and not voided, an attempt on it (voided
    correctness too - he still practised it), or an edge to a taught chapter.

    I3304 -> Protocol layers is reached (its hours, its noted chapters); VLANs is not - its only
    edge goes to the Final, and an exam edge reaches nothing. `proposed` and `unexamined` topics
    are never listed: the plan does not rank the first, and the second carries no exam weight.
    """
    hours = exists().where(hours_entry.c.topic_id == topic.c.id, hours_entry.c.voided_at.is_(None))
    tried = exists().where(attempt.c.topic_id == topic.c.id)
    on_taught_chapter = exists().where(
        coverage.c.topic_id == topic.c.id,
        coverage.c.material_id.in_(_taught_materials(user_id)),
    )
    return select(topic.c.id, topic.c.course_id, topic.c.name).where(
        topic.c.user_id == user_id,
        topic.c.status == "active",
        ~hours,
        ~tried,
        ~on_taught_chapter,
    )


def unconfirmed_by_course(conn: Connection, user_id: str) -> dict[str, dict]:
    """{course_id: {"chapters": [...], "topics": [...]}} for each course that has both an
    untagged taught chapter and an unreached topic (D-267); any other course is absent. Chapters
    and topics are sorted by name.

    I3304 -> {"chapters": [{"material_id": ..., "chapter": "Chapter7_final-LAN Redundancy (STP
    and HSRP)"}], "topics": [{"topic_id": ..., "name": "Distance-vector routing"}, ...]}.
    A course whose taught chapters all carry a tag (I3303's shape), or that has no taught chapter
    yet (I3305's) -> absent.
    """
    chapters: dict[str, list[dict]] = {}
    for row in conn.execute(untagged_taught_chapters(user_id)):
        chapters.setdefault(row.course_id, []).append(
            {"material_id": row.id, "chapter": chapter_name(row.filename)}
        )
    topics: dict[str, list[dict]] = {}
    for row in conn.execute(unreached_topics(user_id)):
        topics.setdefault(row.course_id, []).append({"topic_id": row.id, "name": row.name})
    return {
        course_id: {
            "chapters": sorted(chapters[course_id], key=lambda c: c["chapter"]),
            "topics": sorted(topics[course_id], key=lambda t: t["name"]),
        }
        for course_id in chapters.keys() & topics.keys()
    }
