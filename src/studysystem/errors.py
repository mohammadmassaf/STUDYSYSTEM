"""Structured errors (D-17). Every failure a tool or the dashboard route returns carries a `code`
the caller can branch on, a human `message`, `field_errors` naming the bad inputs, and a `fix`
saying what would resolve it - so the host retries on purpose, not by guessing."""

from sqlalchemy.exc import OperationalError


class StudyError(Exception):
    """The base of every service error. One `field_errors` entry is
    `{"field": <input name>, "problem": <what is wrong with it>}`."""

    def __init__(
        self, code: str, message: str, fix: str, field_errors: list[dict[str, str]] | None = None
    ):
        self.code = code
        self.message = message
        self.field_errors = field_errors if field_errors is not None else []
        self.fix = fix
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "field_errors": list(self.field_errors),
            "fix": self.fix,
        }


def busy(err: OperationalError) -> StudyError:
    """SQLite giving up on its lock wait, as a `StudyError` either front door returns (D-144);
    any other database error re-raises - a crash stays a crash."""
    if "database is locked" not in str(err.orig):
        raise err
    return StudyError(
        code="database_busy",
        message="the database stayed locked: another study program is writing",
        fix="retry in a few seconds",
    )
