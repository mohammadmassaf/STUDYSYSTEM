"""A note's markdown, made harmless before it goes in the student's folder (D-370). The text is
model-written, and a PDF can hide words that make the model write `![](http://...)`: Obsidian loads
it when the note opens, and an address can carry the student's data out.

Outside code, an image becomes `[image: its text]`; a tag, a Templater command and a link that is
not web, email or a place in the notes become plain text. Text in code stays, but a block that an
Obsidian plugin runs becomes plain code. The note in the database stays as it was written.

Where code is, pandoc says: its CommonMark reader gives each piece's place in the source. The result
is read again. When anything live is left, code is not trusted either: it shows its tags escaped.
"""

import html
import json
import re

from studysystem.services.pdf_render import run_pandoc

# What could need a change; a note with none of it is returned as it came.
_SUSPECT = re.compile(r"<[A-Za-z/!?%]|!\[|\]\(|\]:|`\s*\$=")

# The languages of a fence that an Obsidian plugin runs. A run of ticks is read once, whole: read
# again from each of its ticks, a long one takes minutes.
_RUNS = re.compile(
    r"(?<![`~])(`{3,}+|~{3,}+)([ \t]*+)\{?\.?(dataview(js)?|js-engine|customjs|templater|buttons?"
    r"|leaflet|run-[\w-]*|meta-bind[\w-]*)\b[^\n]*",
    re.IGNORECASE,
)
_INLINE_RUNS = re.compile(r"^(`+\s*)\$=")

_MATH = re.compile(r"(?s)\$\$.+?\$\$|(?<![\\$])\$(?![\s$])[^$\n]*?(?<![\s\\])\$(?!\d)")
_EMBED = re.compile(r"!\[\[([^\[\]\n]*)\]\]")
_IMAGE = re.compile(r"!\[([^\[\]\n]*)\]\([^()\n]*\)")
_TARGET = r"\s*(<[^<>\n]*>|[^\s<>]*)"
_LINK = re.compile(r"\]\(" + _TARGET)
_DEFINITION = re.compile(r"\]:" + _TARGET)
_AUTOLINK = re.compile(r"<(?:https?://|mailto:)[^\s<>]*>", re.IGNORECASE)
_TAG = re.compile(r"<(?=[A-Za-z/!?%])")
# ftp too: pandoc makes a link of a bare `ftp://host`, which a networking note can name.
_SCHEMES = ("http", "https", "mailto", "ftp")

READ_SECONDS = 10  # pandoc on one note takes about 0.2 s; a hostile one gets no more


def harmless(body: str) -> str:
    """The text as it may be exported. Never raises."""
    if not (_SUSPECT.search(body) or _RUNS.search(body)):
        return body
    try:
        out = _outside_code(body)
        if not _live(out):
            return out
        out = _neutral(_no_runner(body))
        if not _live(out):
            return out
        # not known to happen: then nothing that starts a tag, an image or a link stays
        return out.replace("<", "&lt;").replace("[", "&#91;")
    except (OSError, RuntimeError, ValueError, RecursionError):
        return _neutral(_no_runner(body))  # pandoc gave no answer: nothing is known to be code


def _outside_code(body: str) -> str:
    """The text with every piece that is not code made harmless."""
    out, at, opened = [], 0, _Opened()
    for start, end in _code(body):
        out.append(_neutral(body[at:start]))
        opened.read(body[at:start])
        piece = body[start:end]
        if opened.hides(piece):
            out.append(_neutral(_no_runner(piece)))
            opened.read(piece)
        else:
            out.append(_INLINE_RUNS.sub(r"\1$ =", _no_runner(piece)))
        at = end
    out.append(_neutral(body[at:]))
    return "".join(out)


class _Opened:
    """What Obsidian may have open where a piece of code starts - math, or a comment - so that it
    reads the code as that, and the math or the comment ends inside it. Only text that is not code
    opens one."""

    def __init__(self):
        self.blocks = self.comments = self.dollars = 0

    def read(self, text: str) -> None:
        self.blocks += text.count("$$")
        self.comments += text.count("%%")
        head, gap, tail = text.rpartition("\n\n")
        if gap:
            self.dollars = 0  # math in a line ends with its paragraph
        self.dollars += tail.replace("$$", "").replace("\\$", "").count("$")

    def hides(self, code: str) -> bool:
        if self.blocks % 2 or self.comments % 2:
            return True
        return "$" in code and self.dollars % 2 == 1


def _no_runner(text: str) -> str:
    return _RUNS.sub(r"\1\2text", text)


def _neutral(text: str) -> str:
    """Text that is not code, with nothing left that runs, loads or opens."""
    out, at = [], 0
    for math in _MATH.finditer(text):
        out.append(_plain(text[at : math.start()]))
        # Harmless read as math and read as text; in math it looks the same.
        inside = _TAG.sub(r"\\lt ", math[0]).replace("![", "! [").replace("](", "] (")
        out.append(inside)
        at = math.end()
    out.append(_plain(text[at:]))
    return "".join(out)


def _plain(text: str) -> str:
    text = _EMBED.sub(r"[image: \1]", text)
    text = _IMAGE.sub(r"[image: \1]", text)
    # what is left of an image: then a link, which is checked next. Never a backslash: the text's
    # own one before it would undo it.
    text = text.replace("![", "!&#91;")
    text = _LINK.sub(_link, text)
    text = _DEFINITION.sub(_link, text)
    out, at = [], 0
    for link in _AUTOLINK.finditer(text):
        out.append(_TAG.sub("&lt;", text[at : link.start()]))
        out.append(link[0])
        at = link.end()
    out.append(_TAG.sub("&lt;", text[at:]))
    return "".join(out)


def _link(match: re.Match) -> str:
    """A link's `](target`, or a definition's `]: target`: kept, or made plain text. `<a b.md>`
    becomes `a%20b.md`, the same place: between angles it would read as a tag."""
    target = match[1]
    if not _safe(target):
        return "]\\" + match[0][1:]
    if not target.startswith("<"):
        return match[0]
    inside = target[1:-1]
    for char, code in ((" ", "%20"), ("\t", "%09"), ("(", "%28"), (")", "%29")):
        inside = inside.replace(char, code)
    return match[0][: -len(target)] + inside


def _safe(target: str) -> bool:
    """Whether a link may keep this address: web, email, or a place in the notes."""
    written = html.unescape(target.strip("<>"))
    for address in (written, re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", written)):
        address = re.sub(r"[\x00-\x20\x7f]", "", address)
        if re.match(r"[/\\]{2}", address):  # a network path
            return False
        scheme = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):", address)
        if scheme and scheme[1].lower() not in _SCHEMES:
            return False
    return True


# --- pandoc's answer ------------------------------------------------------------------------------


def _read(text: str) -> list:
    # The body is never the top of a file, so `---` there opens no settings.
    args = ["--from=gfm-yaml_metadata_block+sourcepos", "--to=json"]
    return json.loads(run_pandoc(args, text, READ_SECONDS))["blocks"]


def _live(text: str) -> bool:
    """Whether a CommonMark reader still finds a tag, an image or an unsafe link."""
    found = False

    def look(node: dict, _place: str | None) -> None:
        nonlocal found
        kind, c = node.get("t"), node.get("c")
        if kind in ("RawInline", "RawBlock", "Image"):
            found = True
        elif kind == "Link" and not _safe(c[2][0]):
            found = True

    _walk(_read(text), look)
    return found


def _code(text: str) -> list[tuple[int, int]]:
    """Where the code is, as (start, end) in the text, in order: each block with its fences, each
    inline piece with its ticks."""
    # Pandoc first joins a letter and its accent into one character, and drops a byte-order mark:
    # its columns then differ from ours. Code starts and ends with ASCII only, so it reads a copy
    # where every other character is the same plain one.
    text = re.sub(r"[^\x00-\x7f]", "\u00e9", text)
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+", text)  # as pandoc ends them
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def offset(place: str) -> int:
        """`line:column`, both from 1; a tab runs to the next fourth column."""
        number, column = map(int, place.split(":"))
        if number > len(lines):
            return len(text)
        if "\t" not in lines[number - 1]:
            return starts[number - 1] + min(column - 1, len(lines[number - 1]))
        at = 1
        for i, char in enumerate(lines[number - 1]):
            if at >= column:
                return starts[number - 1] + i
            at += 4 - (at - 1) % 4 if char == "\t" else 1
        return starts[number]

    found: list[tuple[int, int]] = []

    def look(node: dict, place: str | None) -> None:
        if node.get("t") in ("Code", "CodeBlock") and place is not None:
            for part in place.split(";"):
                start, end = part.split("-")
                found.append((offset(start), offset(end)))

    _walk(_read(text), look)
    found.sort()
    merged: list[tuple[int, int]] = []
    for start, end in found:
        if merged and start < merged[-1][1]:
            continue  # inside one that is there
        merged.append((start, end))
    return merged


def _walk(node, look, place: str | None = None) -> None:
    """Call `look(node, place)` on every element of pandoc's tree; `place` is where it stands."""
    if isinstance(node, list):
        for child in node:
            _walk(child, look, place)
    elif isinstance(node, dict):
        content = node.get("c")
        if node.get("t") in ("Span", "Div", "CodeBlock", "Code", "Link", "Image"):
            place = dict(content[0][2]).get("data-pos", place)
        look(node, place)
        _walk(content, look, place)
