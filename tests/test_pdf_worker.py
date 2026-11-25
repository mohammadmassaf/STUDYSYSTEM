"""The PDF helper (D-366): its limits, the real process that ends at them, and the write lock that a
slow PDF no longer holds."""

import sys
import time

import pytest

from studysystem import demo
from studysystem.errors import StudyError
from studysystem.services import generation, intake, pdf_worker, study_loop
from studysystem.services.generation import start_generation
from studysystem.services.materials import add_material
from tests._papers import pdf_bytes, pdf_pages, write
from tests.test_note_generation import ch6, i3302, web  # noqa: F401 - fixtures

ASK = intake._ask_worker  # the real one: conftest replaces it in every test


@pytest.fixture
def real(monkeypatch):
    monkeypatch.setattr(intake, "_ask_worker", ASK)


def drawn(box: bytes, content: bytes, extra: bytes, objects: list[bytes]) -> bytes:
    """A one-page PDF with an image on it, so the page is drawn."""
    image = (
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray"
        b" /BitsPerComponent 8 /Length 1 >>\nstream\n0\nendstream"
    )
    body = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s] /Contents 4 0 R /Resources"
        b" << /XObject << /I 5 0 R %s >> >> >>" % (box, extra),
        stream(content),
        image,
        *objects,
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, obj in enumerate(body, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    table = b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + (
        b"xref\n0 %d\n0000000000 65535 f \n" % (len(body) + 1)
        + table
        + b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(body) + 1, len(out))
    )


def stream(data: bytes, entries: bytes = b"") -> bytes:
    return b"<< %s /Length %d >>\nstream\n" % (entries, len(data)) + data + b"\nendstream"


def laughs(levels: int = 11) -> bytes:
    """Each form draws the next one ten times: under 3 KB asks for 10^10 drawings."""
    forms = []
    for k in range(levels):
        last = k == levels - 1
        child = b"" if last else b"/Resources << /XObject << /N %d 0 R >> >>" % (7 + k)
        head = b"/Type /XObject /Subtype /Form /BBox [0 0 600 800] " + child
        forms.append(stream(b"0 0 5 5 re f" if last else b"/N Do " * 10, head))
    return drawn(b"600 800", b"/A Do /I Do", b"/A 6 0 R", forms)


def test_a_normal_pdf_comes_back_from_the_real_process(real, tmp_path):
    data = pdf_pages(["Exercise 1", "Exercise 2"], pictured={2})

    pages = intake._pdf_pages(data)

    assert [p.text.strip() for p in pages] == ["Exercise 1", "Exercise 2"]
    assert pages[0].image is None
    assert pages[1].image[:2] == b"\xff\xd8" and pages[1].mime_type == "image/jpeg"


def test_a_tiny_pdf_that_asks_for_all_the_memory_is_refused(real, tmp_path):
    path = write(tmp_path, "laughs.pdf", laughs())

    start = time.monotonic()
    with pytest.raises(StudyError) as caught:
        intake.read([path])

    assert caught.value.code == "invalid_value"
    assert "memory" in caught.value.message
    assert time.monotonic() - start < pdf_worker.MAX_SECONDS + 10


def test_a_pdf_that_never_ends_is_stopped(real, monkeypatch, tmp_path):
    slow = tmp_path / "slow.py"
    slow.write_text("import time; time.sleep(60)")
    monkeypatch.setattr(pdf_worker, "MAX_SECONDS", -3)  # 2 s in all
    run = intake.subprocess.run

    def sleeps(args, **kwargs):
        return run([sys.executable, str(slow)], **kwargs)

    monkeypatch.setattr(intake.subprocess, "run", sleeps)
    start = time.monotonic()

    with pytest.raises(intake._NotRead):
        intake._pdf_pages(pdf_bytes("x"))

    assert time.monotonic() - start < 30


def test_a_helper_that_cannot_start_is_not_the_pdfs_fault(real, monkeypatch):
    run = intake.subprocess.run
    monkeypatch.setattr(
        intake.subprocess,
        "run",
        lambda args, **kw: run([sys.executable, "-c", "import not_a_package_of_ours"], **kw),
    )

    with pytest.raises(StudyError) as caught:
        intake._pdf_pages(pdf_bytes("x"))

    assert caught.value.code == "pdf_helper_failed"


def test_a_reply_that_is_too_long_is_refused(real, monkeypatch, tmp_path):
    script = tmp_path / "w.py"
    script.write_text(
        "from studysystem.services import pdf_worker\n"
        "pdf_worker.MAX_REPLY = 10\n"
        "pdf_worker.main()\n"
    )
    run = intake.subprocess.run
    monkeypatch.setattr(
        intake.subprocess, "run", lambda args, **kw: run([sys.executable, str(script)], **kw)
    )

    with pytest.raises(intake._NotRead, match="too much"):
        intake._pdf_pages(pdf_bytes("x"))


def test_the_helper_ends_itself_at_the_time_limit(tmp_path):
    """Also when the server that started it is gone."""
    script = tmp_path / "w.py"
    script.write_text(
        "import time\n"
        "from studysystem.services import pdf_worker\n"
        "pdf_worker.MAX_SECONDS = 1\n"
        "pdf_worker.read = lambda data: time.sleep(60)\n"
        "pdf_worker.main()\n"
    )
    start = time.monotonic()

    done = intake.subprocess.run([sys.executable, str(script)], input=b"", capture_output=True)

    assert done.returncode != 0
    assert time.monotonic() - start < 30


def test_a_huge_page_is_refused_before_it_is_drawn(tmp_path):
    reply = pdf_worker.read(drawn(b"13000 13000", b"/I Do", b"", []))

    assert "megapixels" in reply["refused"]


def test_too_many_pages_are_refused(monkeypatch):
    monkeypatch.setattr(pdf_worker, "MAX_PAGES", 2)

    assert "3 pages" in pdf_worker.read(pdf_pages(["a", "b", "c"]))["refused"]


def test_a_kept_pdf_that_is_not_read_is_a_structured_refusal(
    service_engine,
    user_id,
    ch6,  # noqa: F811
    monkeypatch,
):
    """Kept before the limits, or on a faster computer: the host reads why (D-36)."""
    monkeypatch.setattr(intake, "_ask_worker", lambda data: {"refused": "took too long"})

    with pytest.raises(StudyError) as caught:
        start_generation(service_engine, user_id, "note", material_id=ch6)

    assert caught.value.code == "pdf_not_read"


def test_another_write_goes_through_while_a_pdf_is_read(
    service_engine,
    user_id,
    ch6,  # noqa: F811
    monkeypatch,
):
    """The stopwatch logs while the host starts a note on a slow PDF."""
    read = generation._read_content
    logged = []

    def logs_meanwhile(file_ref):
        # A connection of its own: inside the start's write unit this would wait, then fail.
        logged.append(study_loop.log_hours(service_engine, user_id, "I3302", 40))
        return read(file_ref)

    monkeypatch.setattr(generation, "_read_content", logs_meanwhile)
    units = []
    unit = generation.write_unit

    def watched(engine):
        units.append(len(logged))
        return unit(engine)

    monkeypatch.setattr(generation, "write_unit", watched)

    task = start_generation(service_engine, user_id, "note", material_id=ch6)

    assert task.payload["content"]
    assert len(logged) == 1  # read once
    assert units == [0, 1]  # the first unit ended before the read; the second one wrote
    assert generation._READ.get() is None


def test_a_summary_still_reads_its_pages(service_engine, user_id, ch6):  # noqa: F811
    from studysystem.services.summaries import summary_task

    assert summary_task(service_engine, user_id, ch6).payload["content"]


def test_a_refused_pdf_leaves_no_row_and_no_file(service_engine, user_id, web, tmp_path):  # noqa: F811
    path = write(tmp_path, "big.pdf", drawn(b"13000 13000", b"/I Do", b"", []))

    with pytest.raises(StudyError):
        add_material(service_engine, user_id, "I3302", path, "chapter")

    assert not intake.papers_dir().exists()


def test_the_demo_reads_its_own_pdfs_with_no_helper(real, monkeypatch, tmp_path):
    """With one, `study demo` took 174 s instead of 6 (measured 2026-11-25)."""
    monkeypatch.setattr(intake.subprocess, "run", lambda *a, **k: pytest.fail("a helper started"))
    monkeypatch.setattr(demo, "seed", lambda engine: len(intake._pdf_pages(pdf_bytes("x"))))

    _, pages, _ = demo.build(tmp_path / "demo")

    assert pages == 1
    assert intake._HELPER.get() is True  # and only there
