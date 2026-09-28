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

import hashlib
import io
import shutil
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from studysystem.db.engine import data_dir
from studysystem.services.values import invalid

IMAGE_TYPES = {".jpg", ".jpeg", ".png"}

# The first bytes of each image type - enough to refuse a renamed .docx without an image library.
MAGIC = {".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".png": b"\x89PNG\r\n\x1a\n"}

SHAPES = "send one PDF, or the paper's page images (.jpg, .png) in page order"


@dataclass(frozen=True)
class Paper:
    """What `read` found: the row's three file columns, and the pages to copy."""

    sha256: str
    file_ref: str
    has_text_layer: bool
    sources: tuple[Path, ...]  # in page order; one entry for a PDF


def papers_dir() -> Path:
    return data_dir() / "papers"


def read(paths: object) -> Paper:
    """Check the files and describe the paper. Raises `invalid_value` on `paths`; writes nothing."""
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise invalid("paths", f"{paths!r} is not a list of file paths", SHAPES)

    files = [Path(p) for p in paths]
    for f in files:
        if not f.is_file():
            raise invalid("paths", f"{f} is not a file", "check the path; " + SHAPES)
    kinds = [f.suffix.lower() for f in files]

    if kinds == [".pdf"]:
        return _read_pdf(files[0])
    if all(k in IMAGE_TYPES for k in kinds):
        return _read_images(files, kinds)
    if ".pdf" in kinds:
        problem = "a PDF cannot be combined with other files"
    else:
        problem = f"{', '.join(sorted(set(kinds) - IMAGE_TYPES))} is not a PDF or an image"
    raise invalid("paths", problem, SHAPES)


def _read_pdf(path: Path) -> Paper:
    data = path.read_bytes()
    try:
        reader = PdfReader(path)
        # One page with real text is enough: a scan has none, a typed paper has it on every page.
        has_text = any((page.extract_text() or "").strip() for page in reader.pages)
    except (PyPdfError, ValueError, OSError):
        raise invalid("paths", f"{path.name} is not a readable PDF", SHAPES) from None
    sha = hashlib.sha256(data).hexdigest()
    return Paper(sha, f"{sha}.pdf", has_text, (path,))


def _read_images(files: list[Path], kinds: list[str]) -> Paper:
    page_shas = []
    for f, kind in zip(files, kinds, strict=True):
        data = f.read_bytes()
        if not data.startswith(MAGIC[kind]):
            raise invalid("paths", f"{f.name} is not a {kind[1:]} image", SHAPES)
        page_shas.append(hashlib.sha256(data).hexdigest())
    if len(set(page_shas)) != len(page_shas):
        raise invalid("paths", "the same page is in the list twice", "send each page once")
    # Sorted: the same photos sent again in another order are still the same paper (D-44).
    sha = hashlib.sha256("".join(sorted(page_shas)).encode()).hexdigest()
    return Paper(sha, f"{sha}/", False, tuple(files))


def keep(paper: Paper) -> list[str]:
    """Write the server's copy if it is not there yet. Returns the stored page names, in order."""
    root = papers_dir()
    if not paper.file_ref.endswith("/"):
        target = root / paper.file_ref
        if not target.exists():
            root.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(paper.sources[0], target)
        return [paper.file_ref]

    folder = root / paper.file_ref.rstrip("/")
    names = [f"{i:02d}{src.suffix.lower()}" for i, src in enumerate(paper.sources, start=1)]
    if not folder.exists():
        # Built beside the real name and renamed in one step, so a crash halfway never leaves a
        # folder that looks complete.
        partial = root / f"{folder.name}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir(parents=True)
        for src, name in zip(paper.sources, names, strict=True):
            shutil.copyfile(src, partial / name)
        partial.rename(folder)
    return names


@dataclass(frozen=True)
class Page:
    """One page on its way out (D-49): its text, a picture of it, or both."""

    text: str | None  # None for a photo - its words are only in the picture
    image: bytes | None  # None when the text is the whole page
    mime_type: str | None


MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

# A PDF page is drawn at 1.5x (108 dpi) and saved as JPEG quality 70: paper 1's figures read
# cleanly at ~150 KB of base64 a page, where 2x costs half as much again (D-49).
RENDER_SCALE = 1.5
JPEG_QUALITY = 70


def read_pages(file_ref: str) -> list[Page]:
    """The kept paper, page by page in page order - how it leaves the server as content, never
    as a path (D-44). A PDF page carries its text, plus a picture when an image sits on it; a
    photo is a picture with no text."""
    if file_ref.endswith("/"):
        folder = papers_dir() / file_ref.rstrip("/")
        return [
            Page(None, f.read_bytes(), MIME[f.suffix.lower()]) for f in sorted(folder.iterdir())
        ]

    path = papers_dir() / file_ref
    reader = PdfReader(path)
    pictured = {i for i, page in enumerate(reader.pages) if _has_image(page)}
    pictures = _render(path, pictured) if pictured else {}
    return [
        Page(
            page.extract_text() or "",
            pictures.get(i),
            "image/jpeg" if i in pictured else None,
        )
        for i, page in enumerate(reader.pages)
    ]


def _has_image(page) -> bool:
    """Whether an image is placed on the page itself. A figure drawn with the PDF's own lines is
    not an image and is missed - D-49's accepted cost."""
    resources = page.get("/Resources")
    xobjects = resources.get_object().get("/XObject") if resources is not None else None
    if xobjects is None:
        return False
    return any(x.get_object().get("/Subtype") == "/Image" for x in xobjects.get_object().values())


def _render(path: Path, indexes: set[int]) -> dict[int, bytes]:
    """JPEG pictures of the pages at `indexes` (from 0)."""
    pdf = pdfium.PdfDocument(path)
    try:
        pictures = {}
        for i in sorted(indexes):
            image = pdf[i].render(scale=RENDER_SCALE).to_pil().convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=JPEG_QUALITY)
            pictures[i] = buffer.getvalue()
        return pictures
    finally:
        pdf.close()
