"""The two values every insert stamps: a ULID primary key (D-32) and a UTC timestamp."""

import datetime

from ulid import ULID


def new_id() -> str:
    """A 26-char ULID. Monotonic within a millisecond, so ids made in order sort in order."""
    return str(ULID())


def now() -> str:
    """The current UTC time as `YYYY-MM-DDTHH:MM:SSZ`, the schema's timestamp format."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
