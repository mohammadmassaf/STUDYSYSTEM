"""Finding a program on this computer. `shutil.which` is not used: on Windows it looks in the
current folder first, and a host can start the server in a folder that someone else filled."""

import os
import sys
from pathlib import Path


def on_path(name: str) -> Path | None:
    """The program `name` in one of PATH's absolute folders, or None."""
    file = f"{name}.exe" if sys.platform == "win32" else name
    for folder in os.get_exec_path():
        path = Path(folder) / file
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return path
    return None
