"""The structured error shape (D-17, D-34): what every tool and the dashboard route return."""

import pytest

from studysystem.db.migrate import SchemaBehindHead
from studysystem.errors import StudyError


def course_exists() -> StudyError:
    return StudyError(
        code="course_exists",
        message="I3302 already exists in Fall 2026",
        fix="use study_set_course_input, or another semester_name",
        field_errors=[{"field": "code", "problem": "taken this semester"}],
    )


def test_to_dict_has_exactly_the_four_keys():
    assert course_exists().to_dict() == {
        "code": "course_exists",
        "message": "I3302 already exists in Fall 2026",
        "field_errors": [{"field": "code", "problem": "taken this semester"}],
        "fix": "use study_set_course_input, or another semester_name",
    }


def test_field_errors_defaults_to_an_empty_list():
    err = StudyError(code="no_user", message="no user yet", fix="run: study migrate")
    assert err.field_errors == []
    assert err.to_dict()["field_errors"] == []


def test_two_errors_do_not_share_one_list():
    a = StudyError(code="a", message="a", fix="a")
    b = StudyError(code="b", message="b", fix="b")
    a.field_errors.append({"field": "x", "problem": "bad"})
    assert b.field_errors == []


def test_raised_and_caught_as_itself_with_the_message_as_text():
    with pytest.raises(StudyError) as excinfo:
        raise course_exists()
    assert str(excinfo.value) == "I3302 already exists in Fall 2026"


def test_schema_behind_head_speaks_the_same_shape():
    err = SchemaBehindHead("0000", "0001")
    assert isinstance(err, StudyError)
    assert err.to_dict() == {
        "code": "schema_behind_head",
        "message": "database is at 0000, head is 0001",
        "field_errors": [],
        "fix": "run: study migrate",
    }
