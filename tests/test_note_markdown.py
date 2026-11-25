"""The exported markdown holds nothing that runs or loads (D-370), and code keeps its text."""

import json
import re
import time

import pytest

from studysystem.services import note_markdown, pdf_render
from studysystem.services.note_markdown import harmless

EVIL = "http://203.0.113.9/leak"


def live(markdown: str) -> list[str]:
    """What a CommonMark reader would still run, load or open, as pandoc reads it."""
    found = []

    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            kind, c = x.get("t"), x.get("c")
            if kind in ("RawInline", "RawBlock") and "html" in c[0]:
                found.append(f"{kind} {c[1]}")
            if kind == "Image":
                found.append(f"Image {c[2][0]}")
            if kind == "Link" and not note_markdown._safe(c[2][0]):
                found.append(f"Link {c[2][0]}")
            walk(c)

    walk(
        json.loads(
            pdf_render.run_pandoc(["--from=gfm-yaml_metadata_block", "--to=json"], markdown)
        )["blocks"]
    )
    return found


ATTACKS = {
    "image": f"![x]({EVIL})",
    "image, nested brackets": f"![a [b] c]({EVIL})",
    "image with code in its text": f"![a `b` c]({EVIL})",
    "image in a link": f"[![x]({EVIL})](https://example.org)",
    "image by reference": f"![x][r]\n\n[r]: {EVIL}",
    "image by its own name": f"![r]\n\n[r]: {EVIL}",
    "image over two lines": f"![x\ny](\n{EVIL})",
    "embed": "![[secret.pdf]]",
    "image, its own backslash": f"\\![x]({EVIL})",
    "image, two backslashes": f"\\\\![x]({EVIL})",
    "image, nested, backslash": f"\\![a [b](c) d]({EVIL})",
    "tag, backslashes": f"\\\\<img src='{EVIL}'>",
    "link, backslashes": "[x\\\\](javascript:alert(1))",
    "a line that only Python ends": f"a \x0b\x0c\x85b `x`\n<img src='{EVIL}'> `y`\n`z`",
    "img": f"<img src='{EVIL}'>",
    "img never closed": f"<img src='{EVIL}'\n\nmore > text",
    "img, upper case": f"<IMG SRC={EVIL}>",
    "script": "<script>\nalert(1)\n</script>",
    "svg": "<svg/onload=alert(1)>",
    "iframe": "<iframe src='//203.0.113.9'></iframe>",
    "style": "<style>@import url(http://203.0.113.9)</style>",
    "comment": f"<!-- -->\n<img src='{EVIL}'>",
    "processing": "<?x ?><b onclick=1>",
    "template": "<% tp.system.exec %>",
    "fence inside pre": f"<pre>\n```\n<img src='{EVIL}'>\n```\n</pre>",
    "fence inside div": f"<div>\n```\n<img src='{EVIL}'>\n```\n</div>",
    "fence after a lone tag": f"<T>\n```\n<img src='{EVIL}'>\n```",
    "fence in a list, then out of it": f"- a\n  ```\n<img src='{EVIL}'>\n  ```",
    "fence in a quote, then out of it": f"> ```\n<img src='{EVIL}'>\n\n```\nx\n```",
    "code over two lines": f"a ` b\nc ` <img src='{EVIL}'> ` d",
    "code across table cells": f"| a | b |\n|---|---|\n| ` | <img src='{EVIL}'> ` |",
    "escaped tick": f"\\`<img src='{EVIL}'>`",
    "ticks of two lengths": f"``a` <img src='{EVIL}'> `b",
    "tag with a tick inside": f"<a href='`'><img src='{EVIL}'>`",
    "javascript": "[x](javascript:alert(1))",
    "javascript, entity": "[x](java&#115;cript:alert(1))",
    "javascript, escape": "[x](javascript\\:alert(1))",
    "javascript in angles": "[x](<javascript:alert(1)>)",
    "file": "[x](file://203.0.113.9/share)",
    "network path": "[x](//203.0.113.9/share)",
    "network path, backslashes": "[x](\\\\\\\\203.0.113.9\\share)",
    "obsidian": "[x](obsidian://advanced-uri?eval=1)",
    "autolink": "<file://203.0.113.9/share>",
    "autolink, custom": "<ms-msdt:/id>",
    "link by reference": "[x][r]\n\n[r]: file://203.0.113.9/share",
    "link by reference, next line": "[x][r]\n\n[r]:\n  <javascript:alert(1)>",
    "tab before code": f"a\tb `x` <img src='{EVIL}'> `y`",
    "no ASCII before code": f"🎯 é `x` <img src='{EVIL}'> `y`",
    "settings at the top": f"---\ntitle: <img src=x>\nimg: ![x]({EVIL})\n---\nbody",
    "code in the settings": f"---\n```\n---\n<img src='{EVIL}'>\n```",
    "fence never closed, then a quote": f"> ```\n> a\nlazy <img src='{EVIL}'>",
    "image over a hard break": f"!\\\n[x]({EVIL})",
    "ftp with more": '<ftp://e.com/"onclick=x> <ftp://e.com>',
    "two-character accents before a tag": f"e\u0301e\u0301e\u0301 `x` <img src='{EVIL}'> `y`",
    "CRLF": f"a `x`\r\n<img src='{EVIL}'>\r\n`y`",
}


@pytest.mark.parametrize("attack", ATTACKS.values(), ids=ATTACKS.keys())
@pytest.mark.parametrize("around", ["{}", "- {}", "text {} text"])
def test_nothing_stays_that_runs_loads_or_opens(attack, around):
    out = harmless(around.format(attack))

    assert live(out) == []
    assert "<%" not in out and "![[" not in out


@pytest.mark.parametrize(
    "math",
    [
        "$a`$ <img src='http://203.0.113.9'> `$b$",
        "$$\n```\n$$\n<img src='http://203.0.113.9'>\n```",
        "%%\n```\n%%\n<img src='http://203.0.113.9'>\n```",
    ],
)
def test_code_that_obsidian_may_read_as_math_or_a_comment_is_not_trusted(math):
    assert "<img" not in harmless(math)


KEPT = {
    "html block": "```html\n<img src='a.png'>\n![x](y)\n[a](file:///x)\n<% x %>\n```",
    "tilde": "~~~jsp\n<%= x %>\n~~~",
    "in a list": "- a\n\n  ```html\n  <div>\n  ```\n- b",
    "in a quote": "> ```html\n> <div>\n> ```",
    "indented": "text\n\n    <div>\n",
    "inline": 'Use `<form action="x">` and `<?php $x ?>` and ``a ` <b>``.',
    "inline in a table": "| a | b |\n|---|---|\n| `<td>` | `<th>` |",
    "inline after code with one dollar": "| `$x` | a |\n|---|---|\n| `$_POST` | `<?php $y ?>` |",
    "accents of two characters": "e\u0301" * 9 + " `<b> and <i>`",
    "Arabic vowel marks": "\u0643\u064e\u062a\u064e\u0628\u064e `<b>` \u0641\u0650\u064a `<i>`",
    "marks and a tab": "e\u0301\tx\u0301 `<b>`",
    "joined emoji": "\U0001f469\u200d\U0001f4bb \u2764\ufe0f `<b>`",
    "a mark first": "\ufeffa `<b>`",
    "settings at the top": "---\ntitle: a\nx: [1]\n---\n\nbody `<b>`",
    "lines like settings that are not": "---\na: & b\n---\n\nbody `<b>`",
    "after an emoji and a tab": "🎯\tthe `<table>` tag",
    "web link": "[x](https://example.org/a_(b)) <https://example.org> [m](mailto:a@b.c) [p](#part)",
    "note links": "[x](other.md) [[Other note]] [[Other#Part|shown]]",
    "math": "$a < b$ and $$x < y$$ and 3 < 4",
    "callout and table": "> [!note] Title\n> text\n\n| a | b |\n|---|---|\n| 1 | 2 |",
    "mermaid": "```mermaid\ngraph TD; A-->B\n```",
    "exclamation": "Careful! [x](https://example.org)",
    "empty": "",
}


@pytest.mark.parametrize("text", KEPT.values(), ids=KEPT.keys())
def test_an_ordinary_note_comes_out_as_it_went_in(text):
    assert harmless(text) == text


def test_an_image_becomes_its_words():
    assert harmless(f"See ![the OSI model]({EVIL}) here") == "See [image: the OSI model] here"
    assert harmless("![[diagram.png]]") == "[image: diagram.png]"


def test_a_tag_in_the_text_shows_as_it_was_written():
    out = harmless("A list of List<T> and <b>bold</b>")

    assert out == "A list of List&lt;T> and &lt;b>bold&lt;/b>"


def test_a_tag_in_math_stays_math():
    assert harmless("$a <b$ <i>") == "$a \\lt b$ &lt;i>"


@pytest.mark.parametrize(
    "lang", ["dataview", "dataviewjs", "DataviewJS", "js-engine", "run-python"]
)
@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_a_block_that_a_plugin_runs_becomes_plain_code(lang, fence):
    out = harmless(f"{fence}{lang}\nrequire('child_process')\n{fence}")

    assert out == f"{fence}text\nrequire('child_process')\n{fence}"


def test_inline_dataview_code_does_not_run():
    assert harmless("a `$= dv.current()` b") == "a `$ = dv.current()` b"


def test_a_second_pass_brings_nothing_back():
    for text in ATTACKS.values():
        assert live(harmless(harmless(text))) == []


def test_with_no_pandoc_everything_is_made_harmless(monkeypatch):
    """The export never fails for this: code then shows its tags escaped."""

    def broken(args, text, seconds):
        raise RuntimeError("pandoc failed")

    monkeypatch.setattr(note_markdown, "run_pandoc", broken)

    out = harmless(f"`<b>` ![x]({EVIL}) <img src=x>")

    assert "<" not in out and "![" not in out


@pytest.mark.parametrize(
    "text",
    [
        "`" * 100_000,
        "~" * 100_000,
        "```" + " " * 100_000,
        "`~" * 50_000,
        "![" * 50_000,
        "](" * 50_000,
        "$" * 100_001,
        "$a " * 30_000,
        "<" * 100_000,
        "[[![" * 25_000,
        "` " * 50_000 + "<b>",
        "`x` " * 25_000 + "<b>",
    ],
    ids=lambda t: t[:6],
)
def test_a_hostile_note_cannot_hold_the_export(text):
    """100,000 ticks took 333 s. Pandoc has its own limit; this is the time without it."""
    start = time.monotonic()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(note_markdown, "run_pandoc", lambda *a: '{"blocks": []}')
        harmless(text)

    assert time.monotonic() - start < 5


def test_pandoc_gets_seconds_not_a_minute(monkeypatch):
    asked = []

    def slow(args, text, seconds):
        asked.append(seconds)
        raise RuntimeError("did not finish")

    monkeypatch.setattr(note_markdown, "run_pandoc", slow)

    assert harmless("a <b>") == "a &lt;b>"
    assert asked == [note_markdown.READ_SECONDS]  # one try, then nothing is trusted to be code
    assert note_markdown.READ_SECONDS <= 10


def test_a_link_to_a_note_with_spaces_goes_to_the_same_place():
    assert harmless("[c](<a b (2).md>)") == "[c](a%20b%20%282%29.md)"
    assert harmless("[c][r]\n\n[r]: <a b.md>") == "[c][r]\n\n[r]: a%20b.md"


@pytest.mark.parametrize(
    "text",
    [
        "e\u0301 `x\u0301y` z",
        "e\u0301te\u0301 x\u0301 `<b>` e\u0301 `<i>`",
        "\u0643\u064e\u062a\u064e `<b>` \u0641\u0650\u064a `<i>`",
        "\u1112\u1161\u11ab `<b>`",
        "\U0001f469\u200d\U0001f4bb `<b>`",
        "\ufeffa `<b>`",
        "a\te\u0301\tb `<b>`",
    ],
    ids=["accents", "words", "Arabic", "Korean", "emoji", "byte-order mark", "tabs"],
)
def test_code_is_found_exactly_where_it_is(text):
    """Pandoc joins some characters before it counts columns."""
    where = [m.span() for m in re.finditer("`[^`]*`", text)]

    assert note_markdown._code(text) == where


def test_a_networking_note_may_name_a_server():
    text = "Use ftp://files.example.org and `<b>` here: [x](other.md)"

    assert harmless(text) == text


def test_a_plain_note_needs_no_pandoc(monkeypatch):
    monkeypatch.setattr(note_markdown, "run_pandoc", lambda *a: pytest.fail("pandoc ran"))

    assert harmless("# Title\n\n- point `code`\n") == "# Title\n\n- point `code`\n"
