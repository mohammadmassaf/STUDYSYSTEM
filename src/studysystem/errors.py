"""Structured errors (D-17). Every failure a tool or the dashboard route returns carries a `code`
the caller can branch on, a human `message`, `field_errors` naming the bad inputs, and a `fix`
saying what would resolve it - so the host retries on purpose, not by guessing."""


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

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "field_errors": self.field_errors,
            "fix": self.fix,
        }
