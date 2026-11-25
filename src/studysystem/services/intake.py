"""The file intake layer (D-44): a paper comes in by local path, is kept as the server's own copy
named by its hash, and is known everywhere else only by `file_ref` - never by the path.

Two steps, so a refusal leaves nothing behind: `read` checks the files and works out everything
the row needs, touching no disk; `keep` writes the copy, and is called inside the write unit just
before the row. A copy whose row then fails is harmless - it is named by its hash, so the retry
finds it already there.

A paper is **one PDF, or one or more page images in page order** (photos downloaded to the
laptop). A PDF is kept as `<sha256>.pdf`; images as a folder `<sha256>/` of `01.jpg`, `02.png`...

The way back out is `read_pages`: generation hands the host the paper's text and pictures, never
a path.
"""

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from studysystem.db.engine import data_dir
from studysystem.errors import StudyError
from studysystem.services import pdf_worker
from studysystem.services.values import invalid

log = logging.getLogger(__name__)

IMAGE_TYPES = {".jpg", ".jpeg", ".png"}

# The first bytes of each image type - enough to refuse a renamed .docx without an image library.
MAGIC = {".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".png": b"\x89PNG\r\n\x1a\n"}

SHAPES = "send one PDF, or the paper's page images (.jpg, .png) in page order"

# D-366: a file past these is refused before it is read. The largest of his 103 PDFs was 2.9 MB.
MAX_FILE = 50_000_000  # bytes
MAX_FILES = pdf_worker.MAX_PAGES
SMALLER = "open the file, print only the pages you need to a new PDF, and send that one"


@dataclass(frozen=True)
class Paper:
    """What `read` found: the row's three file columns, and the pages to copy."""

    sha256: str
    file_ref: str
    has_text_layer: bool
    sources: tuple[Path, ...]  # in page order; one entry for a PDF
    # The bytes `read` hashed, one entry per source: `keep` writes these, never a second read of
    # the path, so the copy is always the file the sha names.
    data: tuple[bytes, ...]


def papers_dir() -> Path:
    return data_dir() / "papers"


def read(paths: object, field: str = "paths") -> Paper:
    """Check the files and describe the paper. Raises `invalid_value` on `field` - the caller's
    name for the input; writes nothing."""
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise invalid(field, f"{paths!r} is not a list of file paths", SHAPES)

    for p in paths:
        _refuse_network_path(p, field)
    files = [Path(p) for p in paths]
    for f in files:
        if not f.is_file():
            raise invalid(field, f"{f} is not a file", "check the path; " + SHAPES)
        if f.stat().st_size > MAX_FILE:
            raise invalid(field, f"{f.name} is over {MAX_FILE // 1_000_000} MB", SMALLER)
    if len(files) > MAX_FILES:
        raise invalid(field, f"{len(files)} files are over the limit of {MAX_FILES}", SHAPES)
    kinds = [f.suffix.lower() for f in files]

    if kinds == [".pdf"]:
        return _read_pdf(files[0], field)
    if all(k in IMAGE_TYPES for k in kinds):
        return _read_images(files, kinds, field)
    if ".pdf" in kinds:
        problem = "a PDF cannot be combined with other files"
    else:
        problem = f"{', '.join(sorted(set(kinds) - IMAGE_TYPES))} is not a PDF or an image"
    raise invalid(field, problem, SHAPES)


def _refuse_network_path(path: str, field: str) -> None:
    """Before anything touches it: on Windows, looking at `\\\\host\\x.pdf` already sends that host
    the student's login (scrambled), and a path can come from text the host read in a PDF."""
    if path.replace("/", "\\").startswith(("\\\\", "\\??\\")):
        raise invalid(
            field,
            f"{path} is a network or device path",
            "copy the file to this computer, then send its path here",
        )


def _read_pdf(path: Path, field: str) -> Paper:
    data = path.read_bytes()
    try:
        pages = _pdf_pages(data)
    except _NotRead as e:
        fix = SHAPES if e.problem == pdf_worker.UNREADABLE else SMALLER
        raise invalid(field, f"{path.name} {e.problem}", fix) from None
    # One page with real text is enough: a scan has none, a typed paper has it on every page.
    has_text = any((page.text or "").strip() for page in pages)
    sha = hashlib.sha256(data).hexdigest()
    return Paper(sha, f"{sha}.pdf", has_text, (path,), (data,))


def _read_images(files: list[Path], kinds: list[str], field: str) -> Paper:
    page_shas, pages = [], []
    for f, kind in zip(files, kinds, strict=True):
        data = f.read_bytes()
        if not data.startswith(MAGIC[kind]):
            raise invalid(field, f"{f.name} is not a {kind[1:]} image", SHAPES)
        page_shas.append(hashlib.sha256(data).hexdigest())
        pages.append(data)
    if len(set(page_shas)) != len(page_shas):
        raise invalid(field, "the same page is in the list twice", "send each page once")
    # Sorted: the same photos sent again in another order are still the same paper (D-44).
    sha = hashlib.sha256("".join(sorted(page_shas)).encode()).hexdigest()
    return Paper(sha, f"{sha}/", False, tuple(files), tuple(pages))


def keep(paper: Paper) -> list[str]:
    """Write the server's copy if it is not there yet. Returns the stored page names, in order."""
    root = papers_dir()
    if not paper.file_ref.endswith("/"):
        target = root / paper.file_ref
        if not target.exists():
            # Written beside the real name and renamed in one step, like the folder below: a
            # copy cut short never sits under the hash name, where the retry would trust it.
            # The pid keeps two processes keeping the same paper off each other's file.
            root.mkdir(parents=True, exist_ok=True)
            partial = root / f"{paper.file_ref}.{os.getpid()}.partial"
            partial.write_bytes(paper.data[0])
            os.replace(partial, target)
        return [paper.file_ref]

    folder = root / paper.file_ref.rstrip("/")
    names = [f"{i:02d}{src.suffix.lower()}" for i, src in enumerate(paper.sources, start=1)]
    if not folder.exists():
        # Built beside the real name and renamed in one step, so a crash halfway never leaves a
        # folder that looks complete.
        partial = root / f"{folder.name}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir(parents=True)
        for data, name in zip(paper.data, names, strict=True):
            (partial / name).write_bytes(data)
        partial.rename(folder)
    return names


@dataclass(frozen=True)
class Page:
    """One page on its way out (D-49): its text, a picture of it, or both."""

    text: str | None  # None for a photo - its words are only in the picture
    image: bytes | None  # None when the text is the whole page
    mime_type: str | None


MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def read_pages(file_ref: str) -> list[Page]:
    """The kept paper, page by page in page order - how it leaves the server as content, never
    as a path (D-44). A PDF page carries its text, plus a picture when an image sits on it; a
    photo is a picture with no text."""
    if file_ref.endswith("/"):
        folder = papers_dir() / file_ref.rstrip("/")
        return [
            Page(None, f.read_bytes(), MIME[f.suffix.lower()]) for f in sorted(folder.iterdir())
        ]

    try:
        return _pdf_pages((papers_dir() / file_ref).read_bytes())
    except _NotRead as e:  # kept before D-366, or on a computer that was faster
        raise StudyError(
            code="pdf_not_read",
            message=f"the kept PDF {e.problem}",
            fix=SHAPES if e.problem == pdf_worker.UNREADABLE else SMALLER,
        ) from None


def page_count(file_ref: str) -> int:
    """How many pages the kept paper has, without drawing any - what `read_pages` would return."""
    if file_ref.endswith("/"):
        return sum(1 for _ in (papers_dir() / file_ref.rstrip("/")).iterdir())
    return len(PdfReader(papers_dir() / file_ref).pages)


_HELPER: ContextVar[bool] = ContextVar("_HELPER", default=True)


@contextmanager
def in_this_process() -> Iterator[None]:
    """Read PDFs with no helper process, which takes about 0.8 s to start. Only for PDFs we made
    ourselves: `study demo` reads over a hundred."""
    token = _HELPER.set(False)
    try:
        yield
    finally:
        _HELPER.reset(token)


class _NotRead(Exception):
    """A PDF the helper did not read: `problem` ends a sentence that starts with the file's name."""

    def __init__(self, problem: str):
        super().__init__(problem)
        self.problem = problem


def _pdf_pages(data: bytes) -> list[Page]:
    """The PDF's pages, read by the helper process (`pdf_worker`, D-366). Raises _NotRead."""
    reply = _ask_worker(data)
    if "refused" in reply:
        raise _NotRead(reply["refused"])
    return [
        Page(
            page["text"],
            None if page["image"] is None else base64.b64decode(page["image"]),
            None if page["image"] is None else "image/jpeg",
        )
        for page in reply["pages"]
    ]


def _ask_worker(data: bytes) -> dict:
    """One PDF to the helper process and its reply back. The tests call the helper's `read` here in
    their own process, except its own tests."""
    if not _HELPER.get():
        return pdf_worker.read(data)
    too_heavy = _NotRead(
        f"took over {pdf_worker.MAX_SECONDS} s or "
        f"{pdf_worker.MAX_MEMORY // 1_000_000_000} GB of memory to read, or broke the reader"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-P", "-m", "studysystem.services.pdf_worker"],
            input=data,  # and so never the MCP messages on the server's own stdin
            capture_output=True,
            timeout=pdf_worker.MAX_SECONDS + 5,  # the helper ends itself first
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise too_heavy from None
    out = done.stdout.decode(errors="replace")
    if not out.startswith(pdf_worker.STARTED.strip()):
        # Ours, not the PDF's: an install with a package missing, a computer that refuses it.
        log.error("the PDF helper: %s", done.stderr.decode(errors="replace")[-2000:])
        raise StudyError(
            code="pdf_helper_failed",
            message="the program that reads PDFs did not start",
            fix="run `uv tool install --reinstall studysystem`, then try again; the log says "
            "why it did not start",
        )
    if done.returncode != 0:
        raise too_heavy
    try:
        return json.loads(out.split("\n", 1)[1])
    except (ValueError, IndexError):
        raise too_heavy from None
