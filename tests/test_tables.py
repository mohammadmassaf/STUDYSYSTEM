"""Migration 001's schema, proved against a real database.

Four things are checked here, the four the task's *Done when* names:

1. every CHECK rejects the row it exists to reject, by name - generically for the
   ULID / bool / enum checks the helpers generate, case by case for the rules;
2. `test_every_check_is_covered` fails the day a CHECK is added without a case, so the
   list above cannot silently fall behind the schema;
3. the exactly-one helper, over every combination of its FKs on all four polymorphic tables;
4. the ownership walk, over the foreign keys read from the metadata - not a hand-written list.

The table list itself is asserted in `test_migrate.py`, against `alembic upgrade head`.
"""

import itertools

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.exc import IntegrityError

from studysystem.db.tables import metadata

X = "2026-09-21T00:00:00Z"
SHA = "a" * 64


def uid(tag: str) -> str:
    """A 26-char id that reads as its tag, so a failure names the row."""
    return (tag.upper() + "0" * 26)[:26]


U, C, SLOT, A, T, T2, M, PE, OBS, EP, GT, SUB, N, MI, PI, PI2, REJ, RQ, SS, PL = (
    uid(x)
    for x in [
        "user", "i3302", "slot", "assess", "topic1", "topic2", "mat", "paper", "obs", "prof",
        "task", "sub", "note", "mem", "item", "item2", "rej", "rq", "sess", "plan",
    ]
)  # fmt: skip

# One valid row per table - the I3302 graph, in foreign-key order. Also the template every
# bad row starts from: a rejection then means the one field the case changed.
SEED: list[tuple[str, dict]] = [
    ("user", dict(id=U, created_at=X)),
    ("course", dict(id=C, user_id=U, code="I3302", name="Databases", semester_name="F26",
                    instructor="Dr. Haddad", instructor_tier="declared",
                    credits=3, credits_tier="declared", target_grade=85,
                    semester_start="2026-09-01", semester_end="2027-01-15", created_at=X)),
    ("class_meeting", dict(id=uid("cm"), user_id=U, course_id=C, weekday=2, start_time="09:00",
                           end_time="10:30", kind="lecture", tier="declared", created_at=X)),
    ("capacity", dict(id=uid("cap"), user_id=U, hours_per_week=20, from_date="2026-09-01",
                      created_at=X)),
    ("assessment_slot", dict(id=SLOT, owner_id=U, course_id=C, name="Final", kind="exam",
                             created_at=X)),
    ("assessment", dict(id=A, user_id=U, slot_id=SLOT, weight=60, weight_tier="declared",
                        date="2027-01-10", session_type="first", created_at=X)),
    ("material", dict(id=M, user_id=U, course_id=C, filename="ch5.pdf", file_ref="f1",
                      content_sha256=SHA, media_type="application/pdf", kind="chapter",
                      added_at=X)),
    ("topic", dict(id=T, user_id=U, course_id=C, name="Normalisation", status="active",
                   proposed_by="material", status_changed_at=X, created_at=X)),
    ("topic", dict(id=T2, user_id=U, course_id=C, name="Indexing", status="proposed",
                   proposed_by="split", status_changed_at=X, created_at=X)),
    ("topic_split", dict(id=uid("split"), user_id=U, parent_topic_id=T, child_topic_id=T2,
                         created_at=X)),
    ("coverage", dict(id=uid("cov"), user_id=U, topic_id=T, material_id=M, tier="declared",
                      created_at=X)),
    ("coverage_reading", dict(id=uid("cr"), user_id=U, course_id=C, value=0.4,
                              denominator_tier="weighted", tier_note="all weights declared",
                              computed_at=X)),
    ("past_exam", dict(id=PE, owner_id=U, slot_id=SLOT, session_type="first",
                       session_date="2025-01-12", instructor_tier="unknown", file_ref="f2",
                       content_sha256=SHA, created_at=X)),
    ("observation", dict(id=OBS, owner_id=U, slot_id=SLOT, past_exam_id=PE,
                         claim_key="topic-marks", claim_class="syllabus-driven", topic_id=T,
                         claim_value='{"marks":12,"of_total":100}', study_tier="E1",
                         citation="Q3", created_at=X)),
    ("exam_profile", dict(id=EP, owner_id=U, slot_id=SLOT, version=1, derived_at=X,
                          provisional_syllabus=1, evidence_count_syllabus=1,
                          evidence_count_instructor=0, evidence_count_format=0)),
    ("topic_weight", dict(id=uid("tw"), exam_profile_id=EP, topic_id=T, weight=100,
                          created_at=X)),
    ("generation_task", dict(id=GT, user_id=U, kind="practice", scope_type="topic", scope_id=T,
                             exam_profile_id=EP, route="host", created_at=X)),
    ("capacity_suggestion", dict(id=uid("cs"), user_id=U, hours_per_week=18, source_task_id=GT,
                                 created_at=X)),
    ("submission", dict(id=SUB, user_id=U, task_id=GT, ordinal=1, payload="{}",
                        validation_result='{"ok":true}', accepted_count=1, rejected_count=0,
                        bytes_sent=0, bytes_returned=10, created_at=X)),
    ("note", dict(id=N, user_id=U, material_id=M, task_id=GT, body="# 3NF", created_at=X)),
    ("memory_item", dict(id=MI, user_id=U, topic_id=T, task_id=GT, item_ordinal=1, term="3NF",
                         definition="no transitive deps", source_marker="from-material",
                         created_at=X)),
    ("practice_item", dict(id=PI, user_id=U, topic_id=T, task_id=GT, item_ordinal=1,
                           origin="past-exam", past_exam_id=PE, position=3,
                           question="Normalise R", answer_provenance="none",
                           source_marker="from-material", created_at=X)),
    ("practice_item", dict(id=PI2, user_id=U, topic_id=T, task_id=GT, item_ordinal=9,
                           origin="generated", question="Decompose S", answer_key="...",
                           answer_provenance="unverified", source_marker="model-knowledge",
                           novelty_score=0.4, created_at=X)),
    ("evidence", dict(id=uid("ev"), user_id=U, observation_id=OBS, study_tier="E1",
                      created_at=X)),
    ("item_rejection", dict(id=REJ, user_id=U, memory_item_id=MI, reason="unclear",
                            created_at=X)),
    ("replacement_queue", dict(id=RQ, user_id=U, topic_id=T, rejection_id=REJ, created_at=X)),
    ("generation_directive", dict(id=uid("gd"), user_id=U, course_id=C,
                                  kind="format-preference", text="fewer MCQs",
                                  source_rejection_id=REJ, created_at=X)),
    ("study_session", dict(id=SS, user_id=U, course_id=C, mode="exam-walkthrough",
                           subject_type="past-exam", subject_id=PE, position=3, source="system",
                           started_at=X, expires_at="2026-09-21T04:00:00Z")),
    ("hours_entry", dict(id=uid("hrs"), user_id=U, course_id=C, session_id=SS, topic_id=T,
                         minutes=45, occurred_at=X, source="timer", created_at=X)),
    ("attempt", dict(id=uid("att"), user_id=U, session_id=SS, practice_item_id=PI, topic_id=T,
                     source="exam-walkthrough", assisted=1, correct=0, root_cause="procedure",
                     fix_rule="check every FD before decomposing", created_at=X)),
    ("discrimination_trial", dict(id=uid("dt"), user_id=U, session_id=SS, practice_item_id=PI,
                                  topic_id=T, was_generated=0, flagged=1, tell="phrasing",
                                  created_at=X)),
    ("topic_state", dict(user_id=U, topic_id=T, reps=1, lapses=1, updated_at=X)),
    ("plan", dict(id=PL, user_id=U, scope="course", scope_course_id=C, shown_n=5,
                  strategy_snapshot="{}", created_at=X)),
    ("plan_item", dict(id=uid("pi1"), user_id=U, plan_id=PL, lane="study", course_id=C,
                       topic_id=T, rank=1, score=0.8, reason="weak on 3NF",
                       estimated_minutes=60, exam_profile_id=EP)),
]  # fmt: skip

# The first row of each table is its template; `topic` and `practice_item` seed a second one.
TEMPLATE: dict[str, dict] = {}
for _name, _row in SEED:
    TEMPLATE.setdefault(_name, _row)

# What a second row of this table must change to clear its unique keys.
UNIQUE_PATCH: dict[str, dict] = {
    "course": dict(code="I3399"),
    "class_meeting": dict(start_time="11:00", end_time="12:30"),
    "assessment_slot": dict(name="Midterm"),
    "assessment": dict(session_type="second"),
    "material": dict(content_sha256="b" * 64),
    "topic": dict(name="Joins"),
    "topic_split": dict(parent_topic_id=T2, child_topic_id=T),
    "coverage": dict(topic_id=T2),
    "past_exam": dict(content_sha256="b" * 64),
    "exam_profile": dict(version=2),
    "topic_weight": dict(topic_id=T2),
    "submission": dict(ordinal=2),
    "note": dict(section_ordinal=2),
    "memory_item": dict(item_ordinal=2),
    "practice_item": dict(item_ordinal=2, position=4),
    "discrimination_trial": dict(practice_item_id=PI2),
    "topic_state": dict(topic_id=T2),
    "plan_item": dict(rank=2),
}


@pytest.fixture
def db(schema_engine):
    """A connection with the I3302 graph on it, inside a transaction that is rolled back.

    The schema is built once per session (see conftest); each test only seeds and undoes its
    own rows, which is what keeps 180 tests off 180 CREATE TABLE runs.
    """
    conn = schema_engine.connect()
    outer = conn.begin()
    for name, row in SEED:
        conn.execute(metadata.tables[name].insert().values(**row))
    yield conn
    outer.rollback()
    conn.close()


def row_for(table: str, **overrides) -> dict:
    """A valid row for `table` that does not collide with the seeded one, plus `overrides`."""
    row = TEMPLATE[table] | UNIQUE_PATCH.get(table, {})
    if "id" in row:
        row = row | {"id": uid("bad")}
    return row | overrides


def insert(conn, table: str, row: dict) -> None:
    """One row, inside a SAVEPOINT - a rollback point within the transaction.

    Postgres aborts the whole transaction on a failed statement and refuses everything after
    it; SQLite does not care. The savepoint is what lets a rejected row leave the seeded
    graph intact on both engines.
    """
    with conn.begin_nested():
        conn.execute(metadata.tables[table].insert().values(**row))


def assert_rejected(conn, table: str, row: dict, constraint: str) -> None:
    """The row must be refused, and by the named constraint - not by a neighbour."""
    with pytest.raises(IntegrityError) as excinfo:
        insert(conn, table, row)
    assert constraint in str(excinfo.value), f"expected {constraint}, got: {excinfo.value}"


# --- the checks the helpers generate --------------------------------------
#
# ULID length, `x IN (0,1)` and `x IN ('a','b')` are written by ulid_pk / bool_col / enum_col,
# so their cases are generated the same way: every one of them gets a bad row, and a new
# column is covered the day it is added.


def _generated_checks() -> list[tuple[str, str, str, object]]:
    """(table, column, constraint name, a value that must be refused) for every CHECK the
    helpers write. They are table-level constraints by the time we see them (`_lift_column_checks`
    in tables.py), so the column is read back out of the constraint's name."""
    cases = []
    for name, table in sorted(metadata.tables.items()):
        for con in table.constraints:
            if not isinstance(con, CheckConstraint) or con.name is None:
                continue
            tail = con.name.removeprefix(f"ck_{name}_")
            if tail.endswith("_ulid"):
                column, bad = "id", "too-short"
            elif tail.endswith("_bool"):
                column, bad = tail.removesuffix("_bool"), 2
            elif tail.endswith("_enum"):
                column, bad = tail.removesuffix("_enum"), "no-such-value"
            else:  # a rule or a range - hand-written below
                continue
            assert column in table.c, f"{con.name} names no column of {name}"
            cases.append((name, column, con.name, bad))
    return cases


GENERATED = _generated_checks()


@pytest.mark.parametrize(
    ("table", "column", "constraint", "bad"), GENERATED, ids=[c[2] for c in GENERATED]
)
def test_generated_check_rejects(db, table, column, constraint, bad):
    assert_rejected(db, table, row_for(table, **{column: bad}), constraint)


# --- the rules ------------------------------------------------------------
#
# (constraint, table, the one thing wrong with the row). Each case changes the smallest set of
# fields that makes exactly its own rule false.

RULE_CASES: list[tuple[str, str, dict]] = [
    # course
    ("ck_course_target_grade_range", "course", dict(target_grade=101)),
    ("ck_course_conceded_at_set", "course", dict(conceded=1)),
    ("ck_course_instructor_tier_rule", "course", dict(instructor_tier="unknown")),
    ("ck_course_credits_tier_rule", "course", dict(credits=None, credits_tier="declared")),
    ("ck_course_credits_range", "course", dict(credits=0)),
    ("ck_course_source_course_v1", "course", dict(source_course_id=C)),
    ("ck_course_semester_order", "course", dict(semester_end="2026-08-01")),
    # assessment
    ("ck_assessment_weight_range", "assessment", dict(weight=101)),
    ("ck_assessment_weight_tier_rule", "assessment", dict(weight=None)),
    ("ck_assessment_approx_needs_date", "assessment", dict(date=None, date_approx=1)),
    ("ck_assessment_mark_range_check", "assessment", dict(mark=101, status="graded")),
    ("ck_assessment_check_graded", "assessment", dict(mark=70, status="sat")),
    # class_meeting
    ("ck_class_meeting_weekday_iso", "class_meeting", dict(weekday=8)),
    ("ck_class_meeting_time_order", "class_meeting", dict(start_time="11:00", end_time="10:00")),
    # capacity
    ("ck_capacity_hours_range", "capacity", dict(hours_per_week=113)),
    ("ck_capacity_suggestion_hours_positive", "capacity_suggestion", dict(hours_per_week=0)),
    ("ck_capacity_suggestion_resolved_when_done", "capacity_suggestion", dict(status="accepted")),
    # material
    ("ck_material_sha256_len", "material", dict(content_sha256="abc")),
    # topic
    ("ck_topic_superseded_by_status", "topic", dict(superseded_by=T, status="active")),
    ("ck_topic_split_not_self", "topic_split", dict(parent_topic_id=T, child_topic_id=T)),
    ("ck_topic_weight_weight_range", "topic_weight", dict(weight=101)),
    # coverage
    ("ck_coverage_reading_value_range", "coverage_reading", dict(value=1.5)),
    # past_exam / observation / exam_profile
    ("ck_past_exam_sha256_len", "past_exam", dict(content_sha256="abc")),
    (
        "ck_past_exam_instructor_tier_rule",
        "past_exam",
        dict(instructor="Dr. Haddad", instructor_tier="unknown"),
    ),
    (
        "ck_observation_claim_class_by_key",
        "observation",
        dict(claim_key="trap", claim_class="format", topic_id=None),
    ),
    (
        "ck_observation_topic_for_marks",
        "observation",
        dict(claim_key="has-mcq", claim_class="format", topic_id=T),
    ),
    ("ck_exam_profile_version_min", "exam_profile", dict(version=0)),
    ("ck_exam_profile_count_syllabus_min", "exam_profile", dict(evidence_count_syllabus=-1)),
    ("ck_exam_profile_count_instructor_min", "exam_profile", dict(evidence_count_instructor=-1)),
    ("ck_exam_profile_count_format_min", "exam_profile", dict(evidence_count_format=-1)),
    # generation_task
    ("ck_generation_task_bytes_sent_min", "generation_task", dict(bytes_sent=-1)),
    ("ck_generation_task_bytes_returned_min", "generation_task", dict(bytes_returned=-1)),
    ("ck_generation_task_superseded_by_status", "generation_task", dict(superseded_by=GT)),
    (
        "ck_generation_task_closed_when_not_open",
        "generation_task",
        dict(status="closed", closed_at=None),
    ),
    # submission / note / memory_item
    ("ck_submission_ordinal_range", "submission", dict(ordinal=4)),
    ("ck_submission_accepted_min", "submission", dict(accepted_count=-1)),
    ("ck_submission_rejected_min", "submission", dict(rejected_count=-1)),
    ("ck_submission_bytes_sent_min", "submission", dict(bytes_sent=-1)),
    ("ck_submission_bytes_returned_min", "submission", dict(bytes_returned=-1)),
    ("ck_note_section_ordinal_min", "note", dict(section_ordinal=0)),
    ("ck_memory_item_item_ordinal_min", "memory_item", dict(item_ordinal=0)),
    # practice_item
    ("ck_practice_item_item_ordinal_min", "practice_item", dict(item_ordinal=0)),
    ("ck_practice_item_position_min", "practice_item", dict(position=0)),
    ("ck_practice_item_past_exam_origin", "practice_item", dict(origin="textbook")),
    ("ck_practice_item_position_past_exam", "practice_item", dict(position=None)),
    ("ck_practice_item_answer_exists", "practice_item", dict(answer_key="R is in 3NF")),
    ("ck_practice_item_marks_min", "practice_item", dict(marks=-1)),
    ("ck_practice_item_past_exam_marker", "practice_item", dict(source_marker="model-knowledge")),
    (
        "ck_practice_item_novelty_range",
        "practice_item",
        dict(origin="generated", past_exam_id=None, position=None, novelty_score=1.5),
    ),
    ("ck_practice_item_novelty_generated", "practice_item", dict(novelty_score=0.5)),
    # item_rejection / replacement_queue / generation_directive
    ("ck_item_rejection_answer_key_is_practice", "item_rejection", dict(reason="wrong-answer-key")),
    ("ck_replacement_queue_filled_by_when_filled", "replacement_queue", dict(filled_by_task_id=GT)),
    ("ck_replacement_queue_resolved_when_done", "replacement_queue", dict(status="dropped")),
    (
        "ck_generation_directive_superseded_by_status",
        "generation_directive",
        dict(status="active", superseded_by=uid("gd"), confirmed_at=X),
    ),
    (
        "ck_generation_directive_confirmed_when_not_proposed",
        "generation_directive",
        dict(status="active"),
    ),
    # study_session / hours_entry / attempt / discrimination_trial
    ("ck_study_session_subject_pair", "study_session", dict(subject_id=None, position=None)),
    ("ck_study_session_position_min", "study_session", dict(position=0)),
    ("ck_study_session_position_walkthrough_only", "study_session", dict(mode="mock")),
    ("ck_study_session_ended_after_start", "study_session", dict(ended_at="2026-09-20T00:00:00Z")),
    ("ck_hours_entry_one_scope", "hours_entry", dict(setup_key="grading:" + C)),
    ("ck_hours_entry_minutes_range", "hours_entry", dict(minutes=721)),
    (
        "ck_attempt_no_session_only_offline",
        "attempt",
        dict(session_id=None, source="practice", assisted=0),
    ),
    ("ck_attempt_walkthrough_is_assisted", "attempt", dict(assisted=0)),
    ("ck_attempt_score_range", "attempt", dict(score=1.5)),
    ("ck_attempt_some_result", "attempt", dict(correct=None, score=None)),
    # topic_state / plan / plan_item
    ("ck_topic_state_reps_min", "topic_state", dict(reps=-1)),
    ("ck_topic_state_lapses_min", "topic_state", dict(lapses=-1)),
    ("ck_plan_shown_n_min", "plan", dict(shown_n=0)),
    ("ck_plan_course_scope_has_course", "plan", dict(scope="all-courses")),
    ("ck_plan_item_rank_min", "plan_item", dict(rank=0)),
    ("ck_plan_item_topic_unless_setup", "plan_item", dict(topic_id=None)),
    ("ck_plan_item_setup_kind_for_setup", "plan_item", dict(setup_kind="grading")),
    ("ck_plan_item_setup_key_for_setup", "plan_item", dict(setup_key="grading:" + C)),
    ("ck_plan_item_score_unless_setup", "plan_item", dict(score=None)),
    ("ck_plan_item_estimated_minutes_min", "plan_item", dict(estimated_minutes=0)),
    (
        "ck_plan_item_setup_has_no_minutes",
        "plan_item",
        dict(
            lane="setup", topic_id=None, score=None, setup_kind="grading", setup_key="grading:" + C
        ),
    ),
    ("ck_plan_item_marks_unlocked_min", "plan_item", dict(marks_unlocked_estimate=-1)),
    ("ck_plan_item_marks_unlocked_setup_only", "plan_item", dict(marks_unlocked_estimate=30)),
]


@pytest.mark.parametrize(("constraint", "table", "bad"), RULE_CASES, ids=[c[0] for c in RULE_CASES])
def test_rule_check_rejects(db, constraint, table, bad):
    assert_rejected(db, table, row_for(table, **bad), constraint)


def _all_check_names() -> set[str]:
    return {
        c.name
        for table in metadata.tables.values()
        for c in table.constraints
        if isinstance(c, CheckConstraint)
    }


def test_every_check_is_covered():
    """Adding a CHECK without a bad row fails here, not silently in six months."""
    covered = {c[2] for c in GENERATED} | {c[0] for c in RULE_CASES}
    covered |= {f"ck_{t}_exactly_one" for t in EXACTLY_ONE}
    assert _all_check_names() - covered == set()


# --- the exactly-one helper -----------------------------------------------

EXACTLY_ONE: dict[str, dict[str, str]] = {
    "coverage": dict(material_id=M, assessment_id=A),
    "evidence": dict(practice_item_id=PI, memory_item_id=MI, observation_id=OBS),
    "item_rejection": dict(practice_item_id=PI, memory_item_id=MI),
    "replacement_queue": dict(rejection_id=REJ, closed_task_id=GT),
}


def _combinations(fks: dict[str, str]):
    """Every subset of the FK set: 4 rows for two columns, 8 for three."""
    for chosen in itertools.product([False, True], repeat=len(fks)):
        pairs = zip(fks.items(), chosen, strict=True)
        yield {col: (val if take else None) for (col, val), take in pairs}


@pytest.mark.parametrize("table", sorted(EXACTLY_ONE), ids=sorted(EXACTLY_ONE))
def test_exactly_one_of_the_fks(db, table):
    fks = EXACTLY_ONE[table]
    seen = 0
    for combo in _combinations(fks):
        seen += 1
        # the other rules of the table must not decide the outcome
        extra = {}
        if table == "item_rejection" and combo["practice_item_id"] is None:
            extra = dict(reason="unclear")
        row = row_for(table, **combo, **extra) | {"id": uid("x" + str(seen))}
        if sum(v is not None for v in combo.values()) == 1:
            insert(db, table, row)  # the single-set rows are the legal ones
        else:
            assert_rejected(db, table, row, f"ck_{table}_exactly_one")
    assert seen == 2 ** len(fks)


# --- ownership ------------------------------------------------------------

OWNER_COLUMNS = ("user_id", "owner_id")
# The two tables that carry no owner column: their owner is reached through their FKs, and the
# service layer proves the two sides agree (physical-schema.md, "Ownership").
OWNERLESS = {"topic_weight", "topic_state"}


def _owner_column(table) -> str | None:
    for name in OWNER_COLUMNS:
        if name in table.c:
            return name
    return None


def test_every_owned_table_carries_an_owner():
    missing = {
        name
        for name, table in metadata.tables.items()
        if name != "user" and name not in OWNERLESS and _owner_column(table) is None
    }
    assert missing == set()


def test_every_foreign_key_reaches_an_owner():
    """The walk the ownership invariant needs: read the FKs from the metadata, so a new one is
    covered the day it is added, and assert each end can name its owner."""
    unreachable = []
    for name, table in sorted(metadata.tables.items()):
        for fk in table.foreign_keys:
            target = fk.column.table.name
            if target == "user":
                continue  # the owner column itself
            if _owner_column(table) is None and name not in OWNERLESS:
                unreachable.append(f"{name}.{fk.parent.name}: source has no owner")
            if _owner_column(fk.column.table) is None and target not in OWNERLESS:
                unreachable.append(f"{name}.{fk.parent.name} -> {target}: target has no owner")
    assert unreachable == []


def test_foreign_keys_restrict_deletes():
    """Nothing in v1 deletes a row with children; retirement is a status change."""
    wrong = [
        f"{t}.{fk.parent.name}"
        for t, table in sorted(metadata.tables.items())
        for fk in table.foreign_keys
        if fk.ondelete != "RESTRICT"
    ]
    assert wrong == []
