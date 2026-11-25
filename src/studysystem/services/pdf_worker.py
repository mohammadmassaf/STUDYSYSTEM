"""Reads one PDF in a process of its own (D-366). A PDF comes from somewhere else, and a tiny one
can ask for all the memory or never end: here that ends this process, not the server.

`intake` starts it, hands it the PDF's bytes on stdin, and reads one JSON object from stdout:
{"pages": [{"text", "image" (base64 JPEG, or null)}]}, or {"refused": why}. It sees no path and no
database.
"""

import base64
import faulthandler
import io
import json
import os
import sys

import pypdfium2 as pdfium
from pypdf import PdfReader

# His 103 PDFs on 2026-11-25: at most 74 pages, 6.4 megapixels a page, 0.6 s a file (D-366).
MAX_PAGES = 300
MAX_PIXELS = 40_000_000  # a page as drawn; 4 bytes each while it is drawn
MAX_SECONDS = 30
MAX_MEMORY = 1_000_000_000  # bytes

# A PDF page is drawn at 1.5x (108 dpi) and saved as JPEG quality 70: paper 1's figures read
# cleanly at ~150 KB of base64 a page, where 2x costs half as much again (D-49).
RENDER_SCALE = 1.5
JPEG_QUALITY = 70

UNREADABLE = "is not a readable PDF"
MAX_REPLY = 200_000_000  # characters: what the server takes back, far over any real file
# Said before the PDF is touched: an end after it is the PDF's doing, one before it is ours.
STARTED = "started\n"


class _Refused(Exception):
    """Why this PDF is not read, as the end of a sentence that starts with its name."""


def read(data: bytes) -> dict:
    """The reply for one PDF. A page is drawn when an image sits on it."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PAGES:
            raise _Refused(f"has {len(reader.pages)} pages, and the limit is {MAX_PAGES}")
        pictured = {i for i, page in enumerate(reader.pages) if _has_image(page)}
        texts = [page.extract_text() or "" for page in reader.pages]
        pictures = _draw(data, pictured) if pictured else {}
    except _Refused as e:
        return {"refused": str(e)}
    except (MemoryError, OSError):
        # The memory limit; on Windows pdfium's own failure arrives as OSError. The process ends,
        # and intake says why.
        raise
    except Exception:
        # Not only PyPdfError: a damaged file reaches pypdf's internals too (KeyError, TypeError,
        # zlib.error...). Still a bad file, not a server bug - it gets the same answer.
        return {"refused": UNREADABLE}
    return {
        "pages": [
            {
                # half an emoji, which a damaged font can give, fits no reply and no database
                "text": text.encode("utf-8", "replace").decode("utf-8"),
                "image": base64.b64encode(pictures[i]).decode() if i in pictures else None,
            }
            for i, text in enumerate(texts)
        ]
    }


def _has_image(page) -> bool:
    """Whether an image is placed on the page itself. A figure drawn with the PDF's own lines is
    not an image and is missed - D-49's accepted cost."""
    resources = page.get("/Resources")
    xobjects = resources.get_object().get("/XObject") if resources is not None else None
    if xobjects is None:
        return False
    return any(x.get_object().get("/Subtype") == "/Image" for x in xobjects.get_object().values())


def _draw(data: bytes, indexes: set[int]) -> dict[int, bytes]:
    """JPEG pictures of the pages at `indexes` (from 0)."""
    pdf = pdfium.PdfDocument(data)
    try:
        pictures = {}
        for i in sorted(indexes):
            page = pdf[i]
            pixels = page.get_width() * page.get_height() * RENDER_SCALE**2
            if pixels > MAX_PIXELS:
                raise _Refused(f"has a page of {pixels / 1e6:,.0f} megapixels, too big to draw")
            image = page.render(scale=RENDER_SCALE).to_pil().convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=JPEG_QUALITY)
            pictures[i] = buffer.getvalue()
        return pictures
    finally:
        pdf.close()


def _limit_memory() -> None:
    """This process gets MAX_MEMORY and, on Windows, starts no other process. macOS refuses the
    limit: there only the time, the page count and the page size hold."""
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no copy of its memory on the disk
        try:
            resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY, MAX_MEMORY))
        except (ValueError, OSError):
            pass
        return

    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("Basic", Basic),
            ("IoInfo", ctypes.c_ulonglong * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    ACTIVE_PROCESS, PROCESS_MEMORY, EXTENDED = 0x8, 0x100, 9
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    limits = Extended()
    limits.Basic.LimitFlags = ACTIVE_PROCESS | PROCESS_MEMORY
    limits.Basic.ActiveProcessLimit = 1
    limits.ProcessMemoryLimit = MAX_MEMORY
    job = kernel.CreateJobObjectW(None, None)
    if not (
        job
        and kernel.SetInformationJobObject(
            job, EXTENDED, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        and kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def main() -> None:
    # A watchdog of its own: it ends the process even while a library never comes back to Python,
    # and also when the server that started it is gone. Its report goes nowhere.
    with open(os.devnull, "w") as nowhere:
        faulthandler.dump_traceback_later(MAX_SECONDS, exit=True, file=nowhere)
        _limit_memory()
        # text: JSON as ASCII, so the pipe's encoding does not matter
        sys.stdout.write(STARTED)
        sys.stdout.flush()
        reply = json.dumps(read(sys.stdin.buffer.read()))
        if len(reply) > MAX_REPLY:
            reply = json.dumps({"refused": "holds too much text and pictures"})
        sys.stdout.write(reply)
        sys.stdout.flush()
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    main()
