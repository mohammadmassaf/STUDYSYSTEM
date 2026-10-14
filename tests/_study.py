"""The installed `study` script, run as its own process the way a host or he starts it."""

import shutil
import subprocess
import sys
from pathlib import Path


def run_study(*args: str) -> subprocess.CompletedProcess:
    """`study <args>` with stdin closed; returns once it exits."""
    script = shutil.which("study", path=str(Path(sys.executable).parent))
    assert script is not None, "the study console script is not installed in this environment"
    return subprocess.run(
        [script, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60
    )
