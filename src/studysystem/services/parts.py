"""Paging a task that is too big for one reply (D-145): a pure function that groups a task's
pieces - its pages and the entries of its long list - into parts, each under the host's
measured budget. It sees sizes only, never content; the caller builds each part from the
indices it returns."""

from dataclasses import dataclass, field

# Measured in Cowork with a probe (D-145): text is cut at 25,000 tokens (~90-100k chars of plain
# text), pictures at a total between 764 and 917 KB. Half the text limit, for French and code.
MAX_CHARS = 50_000
MAX_BYTES = 600_000


@dataclass
class Part:
    """One reply's share: the indices (from 0) of the pages and of the long list's entries it
    carries, each list in the original order."""

    pages: list[int] = field(default_factory=list)
    extras: list[int] = field(default_factory=list)


def split(
    head: int,
    pages: list[tuple[int, int]],
    extras: list[int],
) -> list[Part]:
    """Group the pieces into parts. `head` is the chars of what only part 1 carries (rules,
    schema); `pages` is each page's (text chars, picture bytes), in PDF order, 0 bytes for a page
    with no picture; `extras` is each long-list entry's chars, in order. Always at least one
    part.

    Two passes give what one would, because extras never move a page: pages are placed first in
    every part (D-145 item 6). A piece too big even for an empty part goes alone (item 7); one
    that would fit an empty part waits for the next part instead - so part 1 can be the head
    alone."""
    # Pass 1, pages: each goes in the current part if it fits, else opens the next part.
    parts = [Part()]
    used = [head]  # chars per part, which pass 2 fills up to MAX_CHARS
    size = 0  # picture bytes in the current part
    for i, (chars, picture) in enumerate(pages):
        alone = not parts[-1].pages and (chars > MAX_CHARS or picture > MAX_BYTES)
        if not alone and (used[-1] + chars > MAX_CHARS or size + picture > MAX_BYTES):
            parts.append(Part())
            used.append(0)
            size = 0
        parts[-1].pages.append(i)
        used[-1] += chars
        size += picture

    # Pass 2, extras: poured into the chars each part has left, in order and never going back;
    # one no part has room for opens a new part at the end.
    k = 0
    for j, chars in enumerate(extras):
        while True:
            empty = not parts[k].pages and not parts[k].extras
            if used[k] + chars <= MAX_CHARS or (empty and chars > MAX_CHARS):
                break
            k += 1
            if k == len(parts):
                parts.append(Part())
                used.append(0)
        parts[k].extras.append(j)
        used[k] += chars
    return parts
