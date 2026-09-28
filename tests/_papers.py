"""Tiny paper files for the intake tests, built in code - CI cannot see the vault's PDFs."""

from collections.abc import Collection, Sequence
from pathlib import Path

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16  # only the first bytes are checked
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def pdf_bytes(text: str | None) -> bytes:
    """A valid one-page PDF. With `text`, the page carries it as a text layer; without, the page
    is empty - the shape of a scan, which is pictures and no text."""
    return pdf_pages([text])


def pdf_pages(texts: Sequence[str | None], pictured: Collection[int] = ()) -> bytes:
    """A valid PDF with one page per entry, each carrying its text (or none, like a scan). The
    pages numbered in `pictured` (from 1) also carry an embedded image - a grey square, the shape
    of a figure on a typed paper."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"")  # the page tree, filled in once the pages have numbers
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages = []
    for number, text in enumerate(texts, start=1):
        draw = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        xobjects = b""
        if number in pictured:
            image = add(
                b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray"
                b" /BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream"
            )
            draw += b" q 200 0 0 200 72 400 cm /Im1 Do Q"
            xobjects = b" /XObject << /Im1 %d 0 R >>" % image
        stream = add(b"<< /Length %d >>\nstream\n" % len(draw) + draw + b"\nendstream")
        pages.append(
            add(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R"
                b" /Resources << /Font << /F1 %d 0 R >>%s >> >>" % (stream, font, xobjects)
            )
        )
    kids = b" ".join(b"%d 0 R" % p for p in pages)
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(pages))

    out = b"%PDF-1.4\n"
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return out


def write(folder: Path, name: str, data: bytes) -> str:
    """Write `data` to folder/name and return the path as the host would send it."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return str(path)
