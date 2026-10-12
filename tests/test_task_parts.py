"""A task too big for one reply goes out in parts (D-145): what each part carries, the order the
row enforces, the submit that waits for every part, and the restart when the content moves.
Where the cut falls is `split`'s job (test_parts); here it is fixed at one page per part, the
long list in the last, so each case reads the same on any paper."""

import pytest
from sqlalchemy import func, select

from studysystem.db.tables import generation_task, submission
from studysystem.errors import StudyError
from studysystem.services import generation
from studysystem.services.generation import measure, start_generation, submit_generation
from studysystem.services.parts import Part
from tests.test_generation import figured, web  # noqa: F401 - fixtures
from tests.test_submission import add_topic


@pytest.fixture(autouse=True)
def one_page_a_part(monkeypatch):
    def cut(head, pages, extras):
        parts = [Part(pages=[i]) for i in range(len(pages))]
        parts[-1].extras = list(range(len(extras)))
        return parts

    monkeypatch.setattr(generation, "split", cut)


@pytest.fixture
def topics(service_engine, user_id, web):  # noqa: F811
    """Two topics the transcription may tag to - its long list."""
    return [
        add_topic(service_engine, user_id, web["course_id"], name) for name in ("Regex", "MySQL")
    ]


def part(engine, user_id, paper, n):
    return start_generation(engine, user_id, "transcription", paper, part=n)


def refused(engine, user_id, paper, n) -> StudyError:
    with pytest.raises(StudyError) as excinfo:
        part(engine, user_id, paper, n)
    return excinfo.value


def the_task(engine):
    with engine.connect() as conn:
        return conn.execute(select(generation_task)).one()


# --- what each part carries --------------------------------------------------------------------


def test_part_1_carries_the_rules_and_a_later_part_only_its_pages(
    service_engine,
    user_id,
    figured,  # noqa: F811
    topics,
):
    first = part(service_engine, user_id, figured, 1)
    second = part(service_engine, user_id, figured, 2)
    third = part(service_engine, user_id, figured, 3)

    assert set(first.payload) == {"task_id", "part", "parts", "content", "schema", "rules"}
    assert (first.payload["part"], first.payload["parts"]) == (1, 3)
    assert set(second.payload) == {"task_id", "part", "parts", "content", "rules"}
    assert second.payload["rules"] == {"topics": []}  # its share of the long list: none
    assert [t["id"] for t in third.payload["rules"]["topics"]] == sorted(topics)
    assert [p["page"] for p in second.payload["content"]] == [2]  # page numbers stay the PDF's


def test_a_picture_is_numbered_within_its_part(service_engine, user_id, figured):  # noqa: F811
    # Page 2's figure is picture 1 of part 2, the only picture that reply carries.
    assert part(service_engine, user_id, figured, 1).pictures == []
    second = part(service_engine, user_id, figured, 2)

    assert second.payload["content"][0]["image"] == 1
    assert len(second.pictures) == 1


# --- the order the row keeps -------------------------------------------------------------------


def test_parts_go_out_in_order_and_the_row_counts_them(service_engine, user_id, figured):  # noqa: F811
    sizes = [measure(t.payload, t.pictures) for t in [part(service_engine, user_id, figured, 1)]]
    assert refused(service_engine, user_id, figured, 3).code == "part_out_of_order"
    assert the_task(service_engine).parts_sent == 1  # the refusal recorded nothing

    for n in (2, 3):
        sent = part(service_engine, user_id, figured, n)
        sizes.append(measure(sent.payload, sent.pictures))

    row = the_task(service_engine)
    assert (row.parts, row.parts_sent) == (3, 3)
    assert row.bytes_sent == sum(sizes)


def test_part_1_again_keeps_the_count(service_engine, user_id, figured):  # noqa: F811
    for n in (1, 2, 1):
        part(service_engine, user_id, figured, n)

    assert the_task(service_engine).parts_sent == 2


def test_a_later_part_before_any_task_is_refused_and_writes_nothing(
    service_engine,
    user_id,
    figured,  # noqa: F811
):
    err = refused(service_engine, user_id, figured, 2)

    assert err.field_errors[0]["field"] == "part"
    with service_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(generation_task)).scalar_one() == 0


@pytest.mark.parametrize("bad", [0, 4, 1.5, "two"])
def test_a_part_the_task_does_not_have_is_refused(service_engine, user_id, figured, bad):  # noqa: F811
    part(service_engine, user_id, figured, 1)

    assert refused(service_engine, user_id, figured, bad).field_errors[0]["field"] == "part"


def test_a_part_sent_as_text_counts(service_engine, user_id, figured):  # noqa: F811
    # Cowork may send "2" for 2.
    part(service_engine, user_id, figured, 1)

    assert part(service_engine, user_id, figured, "2").payload["part"] == 2


# --- submit waits for every part ---------------------------------------------------------------


def test_submit_is_refused_until_every_part_went_out(service_engine, user_id, figured):  # noqa: F811
    task_id = part(service_engine, user_id, figured, 1).payload["task_id"]

    with pytest.raises(StudyError) as excinfo:
        submit_generation(service_engine, user_id, task_id, {"items": []})
    assert excinfo.value.code == "parts_missing"
    assert "[2, 3]" in excinfo.value.field_errors[0]["problem"]
    with service_engine.connect() as conn:  # not a submission: none of the three is spent
        assert conn.execute(select(func.count()).select_from(submission)).scalar_one() == 0

    for n in (2, 3):
        part(service_engine, user_id, figured, n)
    reply = submit_generation(service_engine, user_id, task_id, {"items": []})
    assert reply["submission"] == 1


# --- content that moves ------------------------------------------------------------------------


def test_a_later_part_cut_from_changed_content_is_refused_and_part_1_restarts(
    service_engine,
    user_id,
    web,  # noqa: F811
    figured,  # noqa: F811
):
    first = part(service_engine, user_id, figured, 1)
    add_topic(service_engine, user_id, web["course_id"], "Sessions")  # the long list grows

    assert refused(service_engine, user_id, figured, 2).code == "task_changed"

    again = part(service_engine, user_id, figured, 1)
    row = the_task(service_engine)
    assert again.payload["task_id"] == first.payload["task_id"]  # the same task (D-46)
    assert row.parts_sent == 1
    assert part(service_engine, user_id, figured, 2).payload["part"] == 2


# --- through the tool --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [1.5, "two"])
def test_a_bad_part_reaches_the_host_as_a_structured_error(
    service_engine,
    user_id,
    figured,  # noqa: F811
    bad,
):
    # `part` is typed loosely at the tool, so the service's check answers, not the SDK's (D-36).
    import json

    from studysystem.server import create_server
    from tests._client import call_tool

    result = call_tool(
        create_server(service_engine),
        "study_start_generation",
        {"kind": "transcription", "past_exam_id": figured, "part": bad},
    )

    assert result.is_error
    body = json.loads(result.content[0].text)
    assert (body["code"], body["field_errors"][0]["field"]) == ("invalid_value", "part")
