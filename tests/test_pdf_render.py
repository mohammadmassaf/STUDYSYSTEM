"""The PDF render (D-80, D-81): the page pandoc builds for Chrome - stylesheet inside, callout
boxes marked, stamp out of the body - and one real print where this machine has a Chrome."""

import pytest

from studysystem.services.pdf_render import find_chrome, note_page, render_pdf

NOTE = """---
tags: [study-system, note]
generated: true
profile-version: 2
source-task: "01M41M9EFSRY7YJMFKM70NND0E"
---
# Chapter

🎯 likely exam target · 💡 tip · ⚠️ common mistake

- plain point
- 💡 a tip in a list
- another plain point

⚠️ first mistake

⚠️ second mistake

Mid-sentence ⚠️ stays inline.

```php
$x = mysqli_connect();
```

| # | Statement |
|---|---|
| 1 | `mysqli_prepare` |

<script>alert("no")</script>
"""


@pytest.fixture(scope="module")
def page():
    return note_page(NOTE, "PHP_Chapter6_Eng_DataBase_notes")


def test_the_page_carries_its_table_code_and_stylesheet(page):
    assert page.count("<table") == 1
    assert "<pre" in page and "mysqli_connect()" in page
    assert ".callout.warning" in page  # note.css inside: Chrome loads nothing else
    assert "<title>PHP_Chapter6_Eng_DataBase_notes</title>" in page


def test_the_stamp_stays_out_of_the_body(page):
    assert "01M41M9EFSRY7YJMFKM70NND0E" not in page


def test_a_marker_line_becomes_one_box_and_a_run_joins(page):
    assert page.count('class="callout warning"') == 1  # two ⚠️ paragraphs, one box
    assert page.count("Common mistake") == 1
    assert page.count('class="callout tip"') == 1  # lifted out of its list
    assert "a tip in a list" in page


def test_the_legend_and_a_mid_sentence_marker_stay_plain(page):
    assert 'class="callout target"' not in page  # the legend's 🎯 made no box
    assert "Mid-sentence ⚠️ stays inline." in page


def test_raw_html_in_a_note_is_text_never_a_script(page):
    """A note body is model-written: a <script> in it is printed as text, never run."""
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page


@pytest.mark.skipif(find_chrome() is None, reason="no Chrome, Chromium or Edge on this machine")
def test_chrome_prints_a_pdf():
    assert render_pdf(NOTE, "PHP_Chapter6_Eng_DataBase_notes")[:5] == b"%PDF-"


def test_no_chrome_is_an_os_error(monkeypatch):
    """The export turns this into an entry naming the env var - the note itself is safe."""
    monkeypatch.setenv("STUDYSYSTEM_CHROME", "C:/nowhere/chrome.exe")
    with pytest.raises(OSError, match="STUDYSYSTEM_CHROME"):
        render_pdf(NOTE, "x")


def test_an_image_loads_nothing_and_prints_as_text(tmp_path):
    """--embed-resources would read a local file an image names into the page, or fetch a URL:
    a model-written note must pull in neither (D-81)."""
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRETMARKER", encoding="utf-8")
    note = f"![local](<{secret.as_posix()}>)\n\n![remote](http://127.0.0.1:9/x.png)\n"

    page = note_page(note, "x")

    assert "SECRETMARKER" not in page
    assert "127.0.0.1" not in page
    assert "[image: local]" in page and "[image: remote]" in page
