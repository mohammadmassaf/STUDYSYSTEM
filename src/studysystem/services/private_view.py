"""Whether Windows keeps what this process changes private to Claude (D-356, D-358).

A program that the Claude app starts on Windows writes its registry values, and its new AppData
folders, into a copy that only Claude sees. A start at sign-in set there is on for nobody.
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path


def active() -> bool:
    """Whether this process has such a private view. Never off Windows."""
    return sys.platform == "win32" and _new_folders_are_private(local_app_data())


def local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def _new_folders_are_private(parent: Path) -> bool:
    """Whether a new folder made there lands somewhere else. Tested on a real one, removed at once:
    Windows has no reliable way to ask. One that cannot be made or found is unknown, not private."""
    try:
        made = tempfile.mkdtemp(prefix="studysystem-check-", dir=parent)
    except OSError:
        return False
    try:
        inside = os.path.join(os.path.realpath(parent), os.path.basename(made))
        return os.path.normcase(os.path.realpath(made)) != os.path.normcase(inside)
    except OSError:
        return False
    finally:
        # an empty folder left behind, held by a virus scanner say, must not change the answer
        with contextlib.suppress(OSError):
            os.rmdir(made)
