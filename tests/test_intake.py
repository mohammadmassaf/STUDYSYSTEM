"""The file intake layer (D-44): one PDF or page images in, a hash-named copy kept, and the
refusals that must write nothing."""

import pytest

from studysystem.errors import StudyError
from studysystem.services import intake
from tests._papers import JPEG, PNG, pdf_bytes, pdf_pages, write


@pytest.fixture
def downloads(tmp_path):
    """Where the host's paths point - outside the server's data dir."""
    return tmp_path / "downloads"


def refusal(paths) -> StudyError:
    with pytest.raises(StudyError) as excinfo:
        intake.read(paths)
    assert excinfo.value.code == "invalid_value"
    assert [e["field"] for e in excinfo.value.field_errors] == ["paths"]
    return excinfo.value


# --- a PDF ------------------------------------------------------------------


def test_a_typed_pdf_has_a_text_layer(downloads):
    path = write(downloads, "I3302_20192020_First.pdf", pdf_bytes("Exercise 1 - PHP sessions"))

    paper = intake.read([path])

    assert paper.has_text_layer is True
    assert len(paper.sha256) == 64
    assert paper.file_ref == f"{paper.sha256}.pdf"


def test_a_scanned_pdf_has_none(downloads):
    assert intake.read([write(downloads, "scan.pdf", pdf_bytes(None))]).has_text_layer is False


def test_one_path_as_plain_text_is_taken_as_a_list_of_one(downloads):
    path = write(downloads, "p.pdf", pdf_bytes("x"))

    assert intake.read(path) == intake.read([path])


def test_keep_copies_the_pdf_byte_for_byte_under_its_hash(downloads):
    data = pdf_bytes("Exercise 1")
    paper = intake.read([write(downloads, "p.pdf", data)])

    assert intake.keep(paper) == [paper.file_ref]
    assert (intake.papers_dir() / paper.file_ref).read_bytes() == data


def test_keeping_the_same_bytes_twice_leaves_one_copy(downloads):
    data = pdf_bytes("Exercise 1")
    intake.keep(intake.read([write(downloads, "a.pdf", data)]))
    intake.keep(intake.read([write(downloads, "renamed copy.pdf", data)]))

    assert len(list(intake.papers_dir().iterdir())) == 1


def test_read_writes_nothing(downloads):
    intake.read([write(downloads, "p.pdf", pdf_bytes("x"))])

    assert not intake.papers_dir().exists()


# --- page images ------------------------------------------------------------


def pages(downloads, n):
    """n distinct JPEG pages, named as a phone or a download would."""
    return [write(downloads, f"IMG_{i}.jpg", JPEG + bytes([i])) for i in range(1, n + 1)]


def test_photos_are_kept_as_a_folder_of_numbered_pages_in_the_order_given(downloads):
    p1, p2, p3, p4 = pages(downloads, 4)

    paper = intake.read([p3, p1, p2, p4])
    names = intake.keep(paper)

    assert paper.has_text_layer is False
    assert paper.file_ref == f"{paper.sha256}/"
    assert names == ["01.jpg", "02.jpg", "03.jpg", "04.jpg"]
    folder = intake.papers_dir() / paper.sha256
    assert (folder / "01.jpg").read_bytes() == JPEG + bytes([3])  # p3 was sent first


def test_the_same_photos_in_another_order_are_the_same_paper(downloads):
    p1, p2, p3 = pages(downloads, 3)

    assert intake.read([p1, p2, p3]).sha256 == intake.read([p3, p1, p2]).sha256


def test_one_png_page_is_a_paper(downloads):
    paper = intake.read([write(downloads, "page.PNG", PNG)])

    assert intake.keep(paper) == ["01.png"]


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize("paths", [[], None, [3], "", {"path": "x"}])
def test_not_a_list_of_paths_is_refused(paths):
    refusal(paths)


def test_a_missing_file_is_refused(downloads):
    assert "not a file" in refusal([str(downloads / "nope.pdf")]).message


def test_a_folder_is_refused(downloads):
    downloads.mkdir()
    refusal([str(downloads)])


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("paper.docx", b"PK\x03\x04"),  # not a PDF or an image
        ("empty.pdf", b""),
        ("fake.pdf", b"this is text, renamed to .pdf"),
        ("fake.jpg", b"this is text, renamed to .jpg"),
    ],
)
def test_a_file_that_is_not_what_it_claims_is_refused(downloads, name, data):
    refusal([write(downloads, name, data)])


def test_a_pdf_with_other_files_is_refused(downloads):
    pdf = write(downloads, "p.pdf", pdf_bytes("x"))

    assert "cannot be combined" in refusal([pdf, *pages(downloads, 1)]).message


def test_two_pdfs_are_refused(downloads):
    refusal([write(downloads, "a.pdf", pdf_bytes("a")), write(downloads, "b.pdf", pdf_bytes("b"))])


def test_the_same_page_twice_is_refused(downloads):
    (p1,) = pages(downloads, 1)

    assert "twice" in refusal([p1, p1]).message


def test_a_refusal_keeps_nothing(downloads):
    refusal([*pages(downloads, 2), write(downloads, "notes.txt", b"hello")])

    assert not intake.papers_dir().exists()


# --- the way back out (D-49) --------------------------------------------------------


def kept(paths) -> str:
    """Read and keep a paper as add_past_exam would; returns its file_ref."""
    paper = intake.read(paths)
    intake.keep(paper)
    return paper.file_ref


def test_a_typed_pdf_leaves_as_text(downloads):
    ref = kept([write(downloads, "p.pdf", pdf_pages(["page one", "page two"]))])

    pages = intake.read_pages(ref)

    assert [p.text for p in pages] == ["page one", "page two"]
    assert [p.image for p in pages] == [None, None]


def test_a_page_with_a_figure_also_leaves_as_a_jpeg(downloads):
    ref = kept([write(downloads, "p.pdf", pdf_pages(["one", "two", "three"], pictured={2}))])

    pages = intake.read_pages(ref)

    assert [p.image is not None for p in pages] == [False, True, False]
    assert pages[1].mime_type == "image/jpeg"
    assert pages[1].image.startswith(JPEG[:3])
    assert pages[1].text.strip() == "two"  # the text goes too


def test_photos_leave_as_pictures_with_no_text(downloads):
    ref = kept([write(downloads, "1.jpg", JPEG), write(downloads, "2.png", PNG + b"2")])

    pages = intake.read_pages(ref)

    assert [(p.text, p.image, p.mime_type) for p in pages] == [
        (None, JPEG, "image/jpeg"),
        (None, PNG + b"2", "image/png"),
    ]
