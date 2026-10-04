"""The PDF render of a note (D-81): pandoc, bundled inside the `pypandoc-binary` wheel, turns
the stamped markdown into a web page; a headless Chrome (one with no window) prints that page to
PDF - the copy he studies from, opened in Chrome.

The look lives beside the code, in `assets/`: `note.css`, the sheet Chrome prints with (restyle
a note by editing it), and `note-callouts.lua`, the filter that turns a block starting with
⚠️ / 💡 / 🎯 into a box (and `note-no-html.lua`, which keeps a note's raw HTML as text).

Chrome is found on this machine, not installed by us: `STUDYSYSTEM_CHROME` when set, else Chrome,
Chromium or Edge (Chromium-based, on every Windows) in their usual places. Each print runs with
a throwaway profile, so a Chrome window he has open is never touched or handed the job.

This only renders. Writing the bytes - create-only, never over a file (D-73) - is the export's job.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pypandoc

ASSETS = Path(__file__).parent.parent / "assets"
STYLESHEET = ASSETS / "note.css"
CALLOUT_FILTER = ASSETS / "note-callouts.lua"
NO_HTML_FILTER = ASSETS / "note-no-html.lua"

CHROME_ENV = "STUDYSYSTEM_CHROME"
PRINT_TIMEOUT = 60  # seconds - a hung Chrome must not hang the submit with it

_ON_PATH = ("chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
_WINDOWS = (
    r"Google\Chrome\Application\chrome.exe",
    r"Microsoft\Edge\Application\msedge.exe",
)
_MAC = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_chrome() -> Path | None:
    """The Chrome-like browser this machine prints with, or None when it has none."""
    override = os.environ.get(CHROME_ENV, "").strip()
    if override:
        return Path(override) if Path(override).is_file() else None
    for name in _ON_PATH:
        found = shutil.which(name)
        if found:
            return Path(found)
    if sys.platform == "win32":
        roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for root in filter(None, roots):
            for rel in _WINDOWS:
                if (Path(root) / rel).is_file():
                    return Path(root) / rel
    for path in _MAC:
        if Path(path).is_file():
            return Path(path)
    return None


def note_page(stamped_markdown: str, title: str) -> str:
    """The note as the one self-contained web page Chrome prints: the stylesheet inside it, the
    callout boxes marked, the stamp kept out of the body. Raw HTML in the note becomes text
    (`note-no-html.lua`) - a note body is model-written, and a `<script>` in it must never run
    in the printing Chrome."""
    return pypandoc.convert_text(
        stamped_markdown,
        "html5",
        format="gfm+yaml_metadata_block",
        extra_args=[
            "--standalone",
            "--embed-resources",  # the css goes inside the page: Chrome loads nothing else
            f"--css={STYLESHEET}",
            f"--lua-filter={NO_HTML_FILTER}",
            f"--lua-filter={CALLOUT_FILTER}",
            "--syntax-highlighting=none",
            f"--metadata=pagetitle:{title}",
        ],
    )


def render_pdf(stamped_markdown: str, title: str) -> bytes:
    """The note as a PDF file's bytes; `title` is the PDF's title (its file name, no extension).
    Raises RuntimeError when pandoc or Chrome fails on the text, OSError when Chrome or a temp
    file cannot be reached - the caller turns both into an entry."""
    chrome = find_chrome()
    if chrome is None:
        raise OSError(f"no Chrome, Chromium or Edge found - set {CHROME_ENV} to one")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "note.html"
        pdf = Path(tmp) / "note.pdf"
        page.write_text(note_page(stamped_markdown, title), encoding="utf-8")
        try:
            done = subprocess.run(
                [
                    str(chrome),
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--disable-extensions",
                    f"--user-data-dir={Path(tmp) / 'profile'}",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}",
                    page.as_uri(),
                ],
                capture_output=True,
                timeout=PRINT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Chrome did not finish printing in {PRINT_TIMEOUT}s") from e
        if not pdf.is_file() or pdf.stat().st_size == 0:
            said = done.stderr.decode(errors="replace").strip()[-300:]
            raise RuntimeError(f"Chrome printed no PDF (exit {done.returncode}): {said}")
        return pdf.read_bytes()
