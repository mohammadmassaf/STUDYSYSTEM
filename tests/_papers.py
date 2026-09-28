"""Tiny paper files for the intake tests, built in code - CI cannot see the vault's PDFs."""

from pathlib import Path

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16  # only the first bytes are checked
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def pdf_bytes(text: str | None) -> bytes:
    """A valid one-page PDF. With `text`, the page carries it as a text layer; without, the page
    is empty - the shape of a scan, which is pictures and no text."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
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
