"""The 32 tables of physical-schema.md as SQLAlchemy Core metadata.

This is what the code believes the database looks like. Migration 0001 is the frozen copy of it;
`alembic revision --autogenerate` diffs the two. Every construct here must run on SQLite and
Postgres (D-08), so CHECKs use only portable SQL.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)

# Every constraint gets a name, so a later batch migration can drop it by name (D-08). CHECKs
# supply their own short name; the convention prefixes the table.
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "pk": "pk_%(table_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
    }
)

TIERS = ["declared", "inferred", "unknown"]
STUDY_TIERS = ["E1", "E2", "E3"]


# --- column helpers -------------------------------------------------------


def ulid_pk() -> Column:
    """`id TEXT PRIMARY KEY` holding a 26-char ULID."""
    return Column("id", Text, CheckConstraint("length(id) = 26", name="id_ulid"), primary_key=True)


def fk(name: str, target: str, *, nullable: bool = False, index: bool = False) -> Column:
    """A TEXT column referencing `<target>.id`, ON DELETE RESTRICT (nothing deletes; rows retire
    by status)."""
    return Column(
        name, Text, ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable, index=index
    )


def owner(name: str = "user_id") -> Column:
    """The denormalised owner column, indexed. `user_id` on user-owned rows, `owner_id` on the
    evidence cluster (NN in v1, NULL-means-shared in v2)."""

    return fk(name, "user", index=True)


def bool_col(name: str, *, default: int = 0, nullable: bool = False) -> Column:
    """`INTEGER NOT NULL DEFAULT 0, CHECK (x IN (0, 1))`. A nullable bool has no default."""
    return Column(
        name,
        Integer,
        CheckConstraint(f"{name} IN (0,1)", name=f"{name}_bool"),
        server_default=None if nullable else text(str(default)),
        nullable=nullable,
    )


def enum_col(
    name: str, values: list[str], *, nullable: bool = False, default: str | None = None
) -> Column:
    """TEXT + CHECK IN (...). Never a lookup table."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return Column(
        name,
        Text,
        CheckConstraint(f"{name} IN ({quoted})", name=f"{name}_enum"),
        server_default=text(f"'{default}'") if default is not None else None,
        nullable=nullable,
    )


def ts(name: str = "created_at", *, nullable: bool = False) -> Column:
    """ISO-8601 UTC text timestamp."""
    return Column(name, Text, nullable=nullable)


# --- constraint helpers ---------------------------------------------------


def implies(name: str, a: str, b: str) -> CheckConstraint:
    """a => b. Both sides must never evaluate to NULL, or the CHECK is silently skipped -
    use `x IS NOT NULL`, not `x`."""
    return CheckConstraint(f"NOT ({a}) OR ({b})", name=name)


def iff(name: str, a: str, b: str) -> CheckConstraint:
    """a <=> b. Same NULL warning as `implies`."""
    return CheckConstraint(f"(({a}) AND ({b})) OR (NOT ({a}) AND NOT({b}))", name=name)


def tier_pair(field: str, type_, rule: str | None = None) -> list:
    """The value column, its `<field>_tier` column, and the tier rule: unknown <=> NULL.
    `rule` is an extra CHECK on the value (e.g. `credits > 0`); NULL passes it, as CHECKs do.
    Splat into the Table: `*tier_pair("credits", Float, "credits > 0")`."""
    checks = [CheckConstraint(rule, name=f"{field}_range")] if rule is not None else []
    return [
        Column(field, type_, *checks, nullable=True),
        enum_col(f"{field}_tier", TIERS),
        iff(f"{field}_tier_rule", f"{field}_tier = 'unknown'", f"{field} is NULL"),
    ]


def exactly_one(*cols: str) -> CheckConstraint:
    """The portable exactly-one form (D-20): a sum of CASE terms equals 1."""
    terms = " + ".join(f"CASE WHEN {c} IS NULL THEN 0 ELSE 1 END" for c in cols)
    return CheckConstraint(f"{terms} = 1", name="exactly_one")


def partial_unique(name: str, *cols, where: str) -> Index:
    """A unique index over the rows matching `where`, on both engines."""
    return Index(name, *cols, sqlite_where=text(where), postgresql_where=text(where), unique=True)


# --- identity -------------------------------------------------------------

user = Table("user", metadata, ulid_pk(), ts())


# --- course structure -----------------------------------------------------

# TODO(human): `course`. Contract at physical-schema.md "### `course`". Checklist:
#   - ulid_pk, owner, code, name, semester_name, created_at
#   - two tier pairs: instructor (Text, no rule), credits (Float, "> 0")
#   - target_grade 0-100 nullable; strategy enum with default; strategy_changed_at nullable
#   - conceded bool + conceded_at, with the implication conceded = 1 => conceded_at NN
#   - source_course_id: nullable Text, CHECK IS NULL in v1 (a plain CheckConstraint, no FK yet)
#   - semester_start / semester_end dates, semester_end > semester_start when both set
#     (hint: when both set - so what must the CHECK do when one is NULL?)
#   - semester_end_approx bool
#   - unique (user_id, lower(code), semester_name): lower(code) is an expression, so this is an
#     Index(..., unique=True) built after the Table, using func.lower(course.c.code)
course = Table(
    "course",
    metadata,
    ulid_pk(),
    owner(),
    Column("code", Text, nullable=False),
    Column("name", Text, nullable=False),
    *tier_pair("instructor", Text),
    *tier_pair("credits", Float, "credits > 0"),
    Column("target_grade", Float, nullable=True),
    CheckConstraint("target_grade >= 0 AND target_grade <= 100", name="target_grade_range"),
    enum_col("strategy", ["default", "catch-up"], default="default"),
    ts("strategy_changed_at", nullable=True),
    bool_col("conceded"),
    ts("conceded_at", nullable=True),
    implies("conceded_at_set", "conceded = 1", "conceded_at IS NOT NULL"),
    Column("source_course_id", Text, nullable=True),
    CheckConstraint("source_course_id IS NULL", name="source_course_v1"),
    Column("semester_name", Text, nullable=False),
    Column("semester_start", Text, nullable=True),
    Column("semester_end", Text, nullable=True),
    CheckConstraint("semester_end > semester_start", name="semester_order"),
    bool_col("semester_end_approx"),
    ts(),
)
Index(
    "ux_course_code",
    course.c.user_id,
    func.lower(course.c.code),
    course.c.semester_name,
    unique=True,
)

assessment_slot = Table(
    "assessment_slot",
    metadata,
    ulid_pk(),
    owner("owner_id"),
    fk("course_id", "course"),
    Column("name", Text, nullable=False),
    enum_col("kind", ["exam", "project", "lab"]),
    ts("created_at", nullable=False),
)
Index(
    "ux_assessment_slot_name",
    assessment_slot.c.course_id,
    func.lower(assessment_slot.c.name),
    unique=True,
)

assessment = Table(
    "assessment",
    metadata,
    ulid_pk(),
    owner(),
    fk("slot_id", "assessment_slot"),
    *tier_pair("weight", Float, "weight >= 0 AND weight <= 100"),
    Column("date", Text, nullable=True),
    bool_col("date_approx"),
    implies("approx_needs_date", "date_approx = 1", "date IS NOT NULL"),
    Column("time", Text, nullable=True),
    Column("room", Text, nullable=True),
    enum_col("session_type", ["first", "second"], nullable=True),
    enum_col("status", ["upcoming", "sat", "graded"], default="upcoming"),
    Column("mark", Float, nullable=True),
    CheckConstraint("mark >= 0 AND mark <= 100", name="mark_range_check"),
    iff("check_graded", "mark IS NOT NULL", "status = 'graded'"),
    ts("created_at", nullable=False),
    UniqueConstraint("slot_id", "session_type"),
)

Index("ix_assessment_user_date", assessment.c.user_id, assessment.c.date)


class_meeting = Table(
    "class_meeting",
    metadata,
    ulid_pk(),
    owner(),
    fk("course_id", "course"),
    Column(
        "weekday",
        Integer,
        CheckConstraint("weekday >= 1 AND weekday <= 7", name="weekday_iso"),
        nullable=False,
    ),
    Column("start_time", Text, nullable=False),
    Column("end_time", Text, nullable=False),
    CheckConstraint("end_time > start_time", name="time_order"),
    Column("room", Text, nullable=True),
    enum_col("kind", ["lecture", "lab", "tutorial"]),
    enum_col("tier", ["declared", "inferred"]),
    fk("source_task_id", "generation_task", nullable=True),
    ts(),
    UniqueConstraint("course_id", "weekday", "start_time", "kind"),
)

capacity = Table(
    "capacity",
    metadata,
    ulid_pk(),
    owner(),
    Column(
        "hours_per_week",
        Float,
        CheckConstraint("hours_per_week > 0 AND hours_per_week <= 112", name="hours_range"),
        nullable=False,
    ),
    Column("from_date", Text, nullable=False),
    ts(),
)

capacity_suggestion = Table(
    "capacity_suggestion",
    metadata,
    ulid_pk(),
    owner(),
    Column(
        "hours_per_week",
        Float,
        CheckConstraint("hours_per_week > 0", name="hours_positive"),
        nullable=False,
    ),
    fk("source_task_id", "generation_task"),
    enum_col("status", ["pending", "accepted", "dismissed"], default="pending"),
    ts("resolved_at", nullable=True),
    iff("resolved_when_done", "resolved_at IS NOT NULL", "status <> 'pending'"),
    ts(),
)


topic = Table(
    "topic",
    metadata,
    ulid_pk(),
    owner(),
    fk("course_id", "course"),
    Column("name", Text, nullable=False),
    enum_col(
        "status", ["proposed", "active", "unexamined", "superseded", "declined"], nullable=False
    ),
    fk("superseded_by", "topic", nullable=True),
    implies("superseded_by_status", "superseded_by IS NOT NULL", "status = 'superseded'"),
    enum_col("proposed_by", ["material", "profile", "split"]),
    ts("status_changed_at"),
    ts("created_at"),
)

partial_unique(
    "ux_topic_live",
    topic.c.course_id,
    func.lower(topic.c.name),
    where="status IN ('proposed', 'active', 'unexamined')",
)


material = Table(
    "material",
    metadata,
    ulid_pk(),
    owner(),
    fk("course_id", "course"),
    Column("filename", Text, nullable=False),
    Column("file_ref", Text, nullable=False),
    Column(
        "content_sha256",
        Text,
        CheckConstraint("length(content_sha256) = 64", name="sha256_len"),
        nullable=False,
    ),
    Column("media_type", Text, nullable=False),
    bool_col("has_text_layer"),
    enum_col(
        "kind",
        ["slides", "chapter", "lecture-notes", "textbook", "exercises", "syllabus", "other"],
    ),
    ts("added_at"),
    UniqueConstraint("course_id", "content_sha256"),
)

topic_split = Table(
    "topic_split",
    metadata,
    ulid_pk(),
    owner(),
    fk("parent_topic_id", "topic"),
    fk("child_topic_id", "topic"),
    CheckConstraint("child_topic_id <> parent_topic_id", name="not_self"),
    ts(),
    UniqueConstraint("parent_topic_id", "child_topic_id"),
)

# No owner column: its owner is reachable through both FKs (an explicit ownership test case).
topic_weight = Table(
    "topic_weight",
    metadata,
    ulid_pk(),
    fk("exam_profile_id", "exam_profile"),
    fk("topic_id", "topic"),
    Column(
        "weight",
        Float,
        CheckConstraint("weight >= 0 AND weight <= 100", name="weight_range"),
        nullable=False,
    ),
    ts(),
    UniqueConstraint("exam_profile_id", "topic_id"),
)


coverage = Table(
    "coverage",
    metadata,
    ulid_pk(),
    owner(),
    fk("topic_id", "topic"),
    fk("assessment_id", "assessment", nullable=True),
    fk("material_id", "material", nullable=True),
    exactly_one("material_id", "assessment_id"),
    enum_col("tier", ["declared", "inferred"]),
    ts("confirmed_at", nullable=True),
    ts("created_at"),
)


partial_unique(
    "ux_coverage_material",
    coverage.c.topic_id,
    coverage.c.material_id,
    where="material_id IS NOT NULL",
)
partial_unique(
    "ux_coverage_assessment",
    coverage.c.topic_id,
    coverage.c.assessment_id,
    where="assessment_id IS NOT NULL",
)
Index("ix_coverage_topic", coverage.c.topic_id)
Index("ix_coverage_material", coverage.c.material_id)
Index("ix_coverage_user_confirmed", coverage.c.user_id, coverage.c.confirmed_at)


practice_item = Table(
    "practice_item",
    metadata,
    ulid_pk(),
    owner(),
    fk("topic_id", "topic"),
    fk("task_id", "generation_task"),
    Column(
        "item_ordinal",
        Integer,
        CheckConstraint("item_ordinal >= 1", name="item_ordinal_min"),
        nullable=False,
    ),
    enum_col("origin", ["generated", "past-exam", "textbook", "instructor-set"]),
    fk("past_exam_id", "past_exam", nullable=True),
    iff("past_exam_origin", "past_exam_id IS NOT NULL", "origin = 'past-exam'"),
    Column(
        "position", Integer, CheckConstraint("position >= 1", name="position_min"), nullable=True
    ),
    iff("position_past_exam", "position IS NOT NULL", "past_exam_id IS NOT NULL"),
    Column("question", Text, nullable=False),
    Column("answer_key", Text, nullable=True),
    enum_col("answer_provenance", ["verified-source", "unverified", "none"]),
    iff("answer_exists", "answer_provenance = 'none'", "answer_key IS NULL"),
    Column("marks", Float, CheckConstraint("marks >= 0", name="marks_min"), nullable=True),
    enum_col("state", ["live", "needs-review", "rejected", "superseded"], default="live"),
    enum_col("source_marker", ["from-material", "model-knowledge"]),
    implies("past_exam_marker", "origin = 'past-exam'", "source_marker = 'from-material'"),
    Column(
        "novelty_score",
        Float,
        CheckConstraint("novelty_score >= 0 AND novelty_score <= 1", name="novelty_range"),
        nullable=True,
    ),
    iff("novelty_generated", "novelty_score IS NOT NULL", "origin = 'generated'"),
    fk("nearest_item_id", "practice_item", nullable=True),
    ts(),
    UniqueConstraint("task_id", "item_ordinal"),
)
partial_unique(
    "ux_practice_item_position",
    practice_item.c.past_exam_id,
    practice_item.c.position,
    where="past_exam_id IS NOT NULL",
)
Index("ix_practice_item_topic_state", practice_item.c.topic_id, practice_item.c.state)
Index("ix_practice_item_origin_topic", practice_item.c.origin, practice_item.c.topic_id)


evidence = Table(
    "evidence",
    metadata,
    ulid_pk(),
    owner(),
    fk("practice_item_id", "practice_item", nullable=True),
    fk("memory_item_id", "memory_item", nullable=True),
    fk("observation_id", "observation", nullable=True),
    exactly_one("practice_item_id", "memory_item_id", "observation_id"),
    enum_col("study_tier", STUDY_TIERS),
    Column("locator", Text, nullable=True),
    ts("created_at"),
)

hours_entry = Table(
    "hours_entry",
    metadata,
    ulid_pk(),
    owner(),
    fk("course_id", "course"),
    fk("session_id", "study_session", nullable=True),
    fk("topic_id", "topic", nullable=True),
    Column("setup_key", Text, nullable=True),
    # at most one scope; both NULL is the cold-start case and is legal
    CheckConstraint("NOT (topic_id IS NOT NULL AND setup_key IS NOT NULL)", name="one_scope"),
    Column(
        "minutes",
        Integer,
        CheckConstraint("minutes >= 1 AND minutes <= 720", name="minutes_range"),
        nullable=False,
    ),
    ts("occurred_at"),
    enum_col("source", ["tool", "timer", "sweep"]),
    enum_col(
        "deviation_reason",
        ["external-deadline", "disagreed", "too-tired", "topic-wrong", "unexplained"],
        nullable=True,
    ),
    ts("voided_at", nullable=True),
    ts(),
)
Index("ix_hours_entry_user_occurred", hours_entry.c.user_id, hours_entry.c.occurred_at)
Index("ix_hours_entry_user_setup", hours_entry.c.user_id, hours_entry.c.setup_key)


coverage_reading = Table(
    "coverage_reading",
    metadata,
    ulid_pk(),
    owner(),
    fk("course_id", "course"),
    Column(
        "value",
        Float,
        CheckConstraint("value >= 0 AND value <= 1", name="value_range"),
        nullable=False,
    ),
    enum_col("denominator_tier", ["weighted", "inferred", "unweighted"]),
    Column("tier_note", Text, nullable=False),
    ts("computed_at"),
)


# --- evidence cluster: owner_id NN in v1 ------------------------------------

past_exam = Table(
    "past_exam",
    metadata,
    ulid_pk(),
    owner("owner_id"),
    fk("slot_id", "assessment_slot"),
    enum_col("session_type", ["first", "second"]),
    Column("session_date", Text, nullable=False),
    bool_col("session_delayed"),
    *tier_pair("instructor", Text),
    bool_col("has_text_layer"),
    Column("file_ref", Text, nullable=False),
    Column(
        "content_sha256",
        Text,
        CheckConstraint("length(content_sha256) = 64", name="sha256_len"),
        nullable=False,
    ),
    fk("transcript_task_id", "generation_task", nullable=True),
    ts(),
    UniqueConstraint("slot_id", "content_sha256"),
)
Index("ix_past_exam_slot_date", past_exam.c.slot_id, past_exam.c.session_date)

CLAIM_KEYS = {
    "syllabus-driven": ["topic-marks"],
    "format": ["duration-minutes", "total-marks", "question-count", "has-mcq"],
    "instructor-driven": ["archetype", "phrasing", "trap"],
}


def _claim_class_rule() -> str:
    """`claim_class` is fixed by `claim_key`: one OR-branch per class."""
    branches = []
    for cls, keys in CLAIM_KEYS.items():
        quoted = ", ".join(f"'{k}'" for k in keys)
        branches.append(f"(claim_key IN ({quoted}) AND claim_class = '{cls}')")
    return " OR ".join(branches)


observation = Table(
    "observation",
    metadata,
    ulid_pk(),
    owner("owner_id"),
    fk("slot_id", "assessment_slot"),
    fk("past_exam_id", "past_exam", nullable=True),
    enum_col("claim_key", [k for keys in CLAIM_KEYS.values() for k in keys]),
    enum_col("claim_class", list(CLAIM_KEYS)),
    CheckConstraint(_claim_class_rule(), name="claim_class_by_key"),
    fk("topic_id", "topic", nullable=True),
    iff("topic_for_marks", "topic_id IS NOT NULL", "claim_key = 'topic-marks'"),
    Column("claim_value", Text, nullable=False),
    enum_col("study_tier", STUDY_TIERS),
    Column("citation", Text, nullable=True),
    ts(),
)

exam_profile = Table(
    "exam_profile",
    metadata,
    ulid_pk(),
    owner("owner_id"),
    fk("slot_id", "assessment_slot"),
    Column("version", Integer, CheckConstraint("version >= 1", name="version_min"), nullable=False),
    ts("derived_at"),
    bool_col("provisional_syllabus"),
    bool_col("provisional_instructor"),
    bool_col("provisional_format"),
    Column("recency_weight_newest", Integer, server_default=text("3"), nullable=False),
    Column("recency_weight_next_two", Integer, server_default=text("2"), nullable=False),
    Column("recency_weight_older", Integer, server_default=text("1"), nullable=False),
    Column(
        "evidence_count_syllabus",
        Integer,
        CheckConstraint("evidence_count_syllabus >= 0", name="count_syllabus_min"),
        nullable=False,
    ),
    Column(
        "evidence_count_instructor",
        Integer,
        CheckConstraint("evidence_count_instructor >= 0", name="count_instructor_min"),
        nullable=False,
    ),
    Column(
        "evidence_count_format",
        Integer,
        CheckConstraint("evidence_count_format >= 0", name="count_format_min"),
        nullable=False,
    ),
    UniqueConstraint("slot_id", "version"),
)


# --- generation -------------------------------------------------------------

generation_task = Table(
    "generation_task",
    metadata,
    ulid_pk(),
    owner(),
    enum_col("kind", ["note", "practice", "memory", "transcription", "timetable"]),
    # documented discriminator exception: target existence is checked by the service
    enum_col("scope_type", ["course", "material", "topic", "past-exam", "user"]),
    Column("scope_id", Text, nullable=False),
    fk("exam_profile_id", "exam_profile", nullable=True),
    enum_col("route", ["host", "sampling"]),
    Column(
        "bytes_sent",
        Integer,
        CheckConstraint("bytes_sent >= 0", name="bytes_sent_min"),
        server_default=text("0"),
        nullable=False,
    ),
    Column(
        "bytes_returned",
        Integer,
        CheckConstraint("bytes_returned >= 0", name="bytes_returned_min"),
        server_default=text("0"),
        nullable=False,
    ),
    enum_col("status", ["open", "closed", "partial", "superseded", "invalidated"], default="open"),
    fk("superseded_by", "generation_task", nullable=True),
    iff("superseded_by_status", "superseded_by IS NOT NULL", "status = 'superseded'"),
    ts(),
    ts("closed_at", nullable=True),
    iff("closed_when_not_open", "closed_at IS NOT NULL", "status <> 'open'"),
)

submission = Table(
    "submission",
    metadata,
    ulid_pk(),
    owner(),
    fk("task_id", "generation_task"),
    Column(
        "ordinal",
        Integer,
        CheckConstraint("ordinal >= 1 AND ordinal <= 3", name="ordinal_range"),
        nullable=False,
    ),
    Column("payload", Text, nullable=False),
    Column("validation_result", Text, nullable=False),
    Column(
        "accepted_count",
        Integer,
        CheckConstraint("accepted_count >= 0", name="accepted_min"),
        nullable=False,
    ),
    Column(
        "rejected_count",
        Integer,
        CheckConstraint("rejected_count >= 0", name="rejected_min"),
        nullable=False,
    ),
    Column(
        "bytes_sent",
        Integer,
        CheckConstraint("bytes_sent >= 0", name="bytes_sent_min"),
        nullable=False,
    ),
    Column(
        "bytes_returned",
        Integer,
        CheckConstraint("bytes_returned >= 0", name="bytes_returned_min"),
        nullable=False,
    ),
    ts(),
    UniqueConstraint("task_id", "ordinal"),
)

note = Table(
    "note",
    metadata,
    ulid_pk(),
    owner(),
    fk("material_id", "material"),
    fk("task_id", "generation_task"),
    Column(
        "section_ordinal",
        Integer,
        CheckConstraint("section_ordinal >= 1", name="section_ordinal_min"),
        server_default=text("1"),
        nullable=False,
    ),
    fk("exam_profile_id", "exam_profile", nullable=True),
    Column("body", Text, nullable=False),
    Column("exported_path", Text, nullable=True),
    ts(),
    UniqueConstraint("task_id", "section_ordinal"),
)

memory_item = Table(
    "memory_item",
    metadata,
    ulid_pk(),
    owner(),
    fk("topic_id", "topic"),
    fk("task_id", "generation_task"),
    Column(
        "item_ordinal",
        Integer,
        CheckConstraint("item_ordinal >= 1", name="item_ordinal_min"),
        nullable=False,
    ),
    Column("term", Text, nullable=False),
    Column("definition", Text, nullable=False),
    Column("formula", Text, nullable=True),
    enum_col("state", ["live", "needs-review", "rejected", "superseded"], default="live"),
    enum_col("source_marker", ["from-material", "model-knowledge"]),
    ts(),
    UniqueConstraint("task_id", "item_ordinal"),
)

item_rejection = Table(
    "item_rejection",
    metadata,
    ulid_pk(),
    owner(),
    fk("practice_item_id", "practice_item", nullable=True),
    fk("memory_item_id", "memory_item", nullable=True),
    exactly_one("practice_item_id", "memory_item_id"),
    enum_col(
        "reason",
        [
            "off-syllabus",
            "wrong-answer-key",
            "wrong-topic",
            "duplicate",
            "unclear",
            "wrong-difficulty",
        ],
    ),
    implies(
        "answer_key_is_practice", "reason = 'wrong-answer-key'", "practice_item_id IS NOT NULL"
    ),
    Column("note", Text, nullable=True),
    ts(),
)

replacement_queue = Table(
    "replacement_queue",
    metadata,
    ulid_pk(),
    owner(),
    fk("topic_id", "topic"),
    fk("rejection_id", "item_rejection", nullable=True),
    fk("closed_task_id", "generation_task", nullable=True),
    exactly_one("rejection_id", "closed_task_id"),
    enum_col("status", ["pending", "filled", "dropped"], default="pending"),
    fk("filled_by_task_id", "generation_task", nullable=True),
    iff("filled_by_when_filled", "filled_by_task_id IS NOT NULL", "status = 'filled'"),
    ts(),
    ts("resolved_at", nullable=True),
    iff("resolved_when_done", "resolved_at IS NOT NULL", "status <> 'pending'"),
)
Index("ix_replacement_queue_user_status", replacement_queue.c.user_id, replacement_queue.c.status)

generation_directive = Table(
    "generation_directive",
    metadata,
    ulid_pk(),
    owner(),
    fk("course_id", "course", nullable=True),
    fk("topic_id", "topic", nullable=True),
    enum_col("kind", ["format-preference", "generator-correction"]),
    Column("text", Text, nullable=False),
    enum_col("status", ["proposed", "active", "superseded"], default="proposed"),
    fk("superseded_by", "generation_directive", nullable=True),
    iff("superseded_by_status", "superseded_by IS NOT NULL", "status = 'superseded'"),
    fk("source_rejection_id", "item_rejection", nullable=True),
    ts(),
    ts("confirmed_at", nullable=True),
    iff("confirmed_when_not_proposed", "confirmed_at IS NOT NULL", "status <> 'proposed'"),
)
Index(
    "ix_generation_directive_user_status",
    generation_directive.c.user_id,
    generation_directive.c.status,
)
Index(
    "ix_generation_directive_course_status",
    generation_directive.c.course_id,
    generation_directive.c.status,
)


# --- study and performance ----------------------------------------------------

study_session = Table(
    "study_session",
    metadata,
    ulid_pk(),
    owner(),
    fk("course_id", "course"),
    enum_col("mode", ["explain-chapter", "exam-walkthrough", "mock", "review-quiz"]),
    # documented discriminator exception: target existence is checked by the service
    enum_col("subject_type", ["topic", "material", "past-exam"], nullable=True),
    Column("subject_id", Text, nullable=True),
    iff("subject_pair", "subject_id IS NULL", "subject_type IS NULL"),
    Column(
        "position", Integer, CheckConstraint("position >= 1", name="position_min"), nullable=True
    ),
    implies("position_walkthrough_only", "position IS NOT NULL", "mode = 'exam-walkthrough'"),
    enum_col("source", ["system", "manual"]),
    ts("started_at"),
    ts("expires_at"),
    ts("ended_at", nullable=True),
    CheckConstraint("ended_at >= started_at", name="ended_after_start"),
)

attempt = Table(
    "attempt",
    metadata,
    ulid_pk(),
    owner(),
    fk("session_id", "study_session", nullable=True),
    implies(
        "no_session_only_offline",
        "session_id IS NULL",
        "source IN ('self-reported', 'exam-retro')",
    ),
    fk("practice_item_id", "practice_item", nullable=True),
    fk("topic_id", "topic"),
    enum_col(
        "source",
        [
            "comprehension-check",
            "practice",
            "review-quiz",
            "exam-walkthrough",
            "exam-retro",
            "self-reported",
        ],
    ),
    bool_col("assisted"),
    implies("walkthrough_is_assisted", "source = 'exam-walkthrough'", "assisted = 1"),
    bool_col("correct", nullable=True),
    Column(
        "score",
        Float,
        CheckConstraint("score >= 0 AND score <= 1", name="score_range"),
        nullable=True,
    ),
    CheckConstraint("correct IS NOT NULL OR score IS NOT NULL", name="some_result"),
    enum_col(
        "root_cause",
        ["concept", "recall", "procedure", "misread", "careless", "time-pressure"],
        nullable=True,
    ),
    Column("fix_rule", Text, nullable=True),
    bool_col("correctness_voided"),
    ts(),
)
Index("ix_attempt_user_topic", attempt.c.user_id, attempt.c.topic_id)

discrimination_trial = Table(
    "discrimination_trial",
    metadata,
    ulid_pk(),
    owner(),
    fk("session_id", "study_session"),
    fk("practice_item_id", "practice_item"),
    fk("topic_id", "topic"),
    bool_col("was_generated"),
    bool_col("flagged"),
    enum_col(
        "tell", ["too-easy", "phrasing", "topic-scope", "formatting", "no-tell"], nullable=True
    ),
    ts(),
    UniqueConstraint("session_id", "practice_item_id"),
)
Index(
    "ix_discrimination_trial_user_generated",
    discrimination_trial.c.user_id,
    discrimination_trial.c.was_generated,
)

# The one natural key: no ULID, PK (user_id, topic_id).
topic_state = Table(
    "topic_state",
    metadata,
    Column("user_id", Text, ForeignKey("user.id", ondelete="RESTRICT"), primary_key=True),
    Column("topic_id", Text, ForeignKey("topic.id", ondelete="RESTRICT"), primary_key=True),
    Column("stability", Float, nullable=True),
    Column("difficulty", Float, nullable=True),
    ts("due_at", nullable=True),
    ts("last_reviewed_at", nullable=True),
    Column("reps", Integer, CheckConstraint("reps >= 0", name="reps_min"), nullable=False),
    Column("lapses", Integer, CheckConstraint("lapses >= 0", name="lapses_min"), nullable=False),
    bool_col("inferred"),
    ts("updated_at"),
)
Index("ix_topic_state_user_due", topic_state.c.user_id, topic_state.c.due_at)


# --- planning -------------------------------------------------------------------

plan = Table(
    "plan",
    metadata,
    ulid_pk(),
    owner(),
    enum_col("scope", ["all-courses", "course"]),
    fk("scope_course_id", "course", nullable=True),
    iff("course_scope_has_course", "scope_course_id IS NOT NULL", "scope = 'course'"),
    Column("shown_n", Integer, CheckConstraint("shown_n >= 1", name="shown_n_min"), nullable=False),
    Column("strategy_snapshot", Text, nullable=False),
    ts(),
)

SETUP_KINDS = ["grading", "credits", "exam-date", "target-grade", "first-material", "past-papers"]

plan_item = Table(
    "plan_item",
    metadata,
    ulid_pk(),
    owner(),
    fk("plan_id", "plan"),
    enum_col("lane", ["review", "study", "setup"]),
    fk("course_id", "course"),
    fk("topic_id", "topic", nullable=True),
    iff("topic_unless_setup", "topic_id IS NOT NULL", "lane <> 'setup'"),
    enum_col("setup_kind", SETUP_KINDS, nullable=True),
    iff("setup_kind_for_setup", "setup_kind IS NOT NULL", "lane = 'setup'"),
    Column("setup_key", Text, nullable=True),
    iff("setup_key_for_setup", "setup_key IS NOT NULL", "lane = 'setup'"),
    Column("rank", Integer, CheckConstraint("rank >= 1", name="rank_min"), nullable=False),
    bool_col("shown"),
    Column("score", Float, nullable=True),
    iff("score_unless_setup", "score IS NOT NULL", "lane <> 'setup'"),
    Column("reason", Text, nullable=False),
    Column(
        "estimated_minutes",
        Integer,
        CheckConstraint("estimated_minutes > 0", name="estimated_minutes_min"),
        nullable=True,
    ),
    implies("setup_has_no_minutes", "lane = 'setup'", "estimated_minutes IS NULL"),
    Column(
        "marks_unlocked_estimate",
        Float,
        CheckConstraint("marks_unlocked_estimate >= 0", name="marks_unlocked_min"),
        nullable=True,
    ),
    implies("marks_unlocked_setup_only", "marks_unlocked_estimate IS NOT NULL", "lane = 'setup'"),
    fk("exam_profile_id", "exam_profile", nullable=True),
    UniqueConstraint("plan_id", "lane", "rank"),
)
Index("ix_plan_item_user_topic", plan_item.c.user_id, plan_item.c.topic_id)
Index("ix_plan_item_user_setup", plan_item.c.user_id, plan_item.c.setup_key)


# --- one last pass over the metadata ----------------------------------------


def _lift_column_checks() -> None:
    """Move every CHECK written on a Column onto its Table.

    `alembic revision --autogenerate` renders table-level CHECKs and silently drops
    column-level ones, so without this, Migration 001 would build a database with 47 of the
    schema's 175 CHECKs - and the tests, which run on this metadata, would never notice.
    Moving them changes no constraint name and no emitted SQL; it only changes where
    autogenerate can see them. They are re-appended ahead of the table's own rules, keeping
    the order they had as column constraints - SQLite reports the *first* CHECK a row fails,
    and the tests name the one they expect.
    """
    for table in metadata.tables.values():
        for col in table.c:
            for con in list(col.constraints):
                if not isinstance(con, CheckConstraint):
                    continue
                col.constraints.discard(con)
                moved = CheckConstraint(con.sqltext, name=con.name)
                # CREATE TABLE emits constraints in creation order, and SQLite reports the
                # first CHECK a row fails. Inheriting the original's order keeps every
                # failure named the same as when the CHECK sat on the column.
                moved._creation_order = con._creation_order
                table.append_constraint(moved)


_lift_column_checks()
