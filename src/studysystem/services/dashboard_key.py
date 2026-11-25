"""The dashboard's secret (D-369). The port answers every program on this computer, also those of
another person who is signed in; the secret lies in the data folder, which only the student's own
account reads.

The secret itself never leaves the folder. Each use gets its own value made from it:
- `token("browser")` - what a browser keeps and sends with every request;
- `token("stop")` - what `study stop` sends;
- `code()` - the one-time part of the link that the study system opens: the dashboard gives the
  browser its token for it, once, within CODE_SECONDS;
- `proof(nonce)` - how a dashboard shows the launcher that it serves this data folder.
"""

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from studysystem.db.engine import data_dir
from studysystem.errors import StudyError

FILE = "dashboard.key"
HEADER = "X-Study-Key"
CODE_SECONDS = 600
MAX_NONCE = 64


def key() -> bytes:
    """This data folder's secret, made the first time. Refused: dashboard_key_unreadable."""
    path = data_dir() / FILE
    try:
        return _read_or_make(path)
    except (OSError, ValueError) as e:
        raise StudyError(
            code="dashboard_key_unreadable",
            message=f"the dashboard's key file {path} cannot be used: {e}",
            fix="check that your account can read and write that folder; if the file is damaged, "
            "delete it, sign out of the computer and in again, and start the dashboard",
        ) from None


def _read_or_make(path: Path) -> bytes:
    try:
        # 0o600: on Linux and macOS only this account reads it. "x": two starts never make two.
        made = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(made, "w") as f:
            f.write(secrets.token_hex(32))
    for _ in range(50):
        found = path.read_text().strip()
        if len(found) == 64:
            return bytes.fromhex(found)
        time.sleep(0.02)  # another start made the file just now and still writes it
    raise ValueError("it holds no key")


def _make(secret: bytes, purpose: str) -> str:
    return hmac.new(secret, purpose.encode(), hashlib.sha256).hexdigest()


def token(purpose: str, secret: bytes | None = None) -> str:
    return _make(secret or key(), f"token:{purpose}")


def proof(nonce: str, secret: bytes | None = None) -> str:
    return _make(secret or key(), f"proof:{nonce}")


def code() -> str:
    ends = int(time.time()) + CODE_SECONDS
    nonce = secrets.token_hex(16)
    return f"{ends}.{nonce}.{_make(key(), f'code:{ends}.{nonce}')}"


class Codes:
    """The one-time codes a running dashboard has taken."""

    def __init__(self, secret: bytes):
        self._secret = secret
        self._used: dict[str, int] = {}

    def take(self, text: object) -> bool:
        """Whether this code is ours, still in time and new. It is used up by a yes."""
        if not isinstance(text, str) or text.count(".") != 2 or not text.isascii():
            return False
        ends, nonce, mark = text.split(".")
        wanted = _make(self._secret, f"code:{ends}.{nonce}")
        if not hmac.compare_digest(mark, wanted):
            return False
        now = time.time()
        self._used = {n: e for n, e in self._used.items() if e > now}
        if int(ends) <= now or nonce in self._used:
            return False
        self._used[nonce] = int(ends)
        return True


def same(sent: str | None, wanted: str) -> bool:
    return sent is not None and sent.isascii() and hmac.compare_digest(sent, wanted)
