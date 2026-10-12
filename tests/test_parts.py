"""Paging a task too big for one reply (D-145): `split` is pure, so every case is sizes in, part
indices out. The real cases are I3304 chapter 1 part 2, measured from the live task on
2026-10-11."""

from studysystem.services.parts import MAX_BYTES, MAX_CHARS, Part, split

# Each page's (text chars, picture base64 bytes), in PDF order.
DECK = [
    (780, 139328),
    (833, 146516),
    (990, 133588),
    (1026, 126388),
    (825, 145604),
    (277, 85252),
    (498, 122968),
    (794, 143708),
    (793, 144380),
    (637, 114644),
]
# Tagging: the 18 active topics, each with all its past-exam questions (D-104), in payload order.
TOPICS = [36277, 3045, 2353, 18458, 5532, 8669, 6875, 1480, 17269, 2693, 8606, 1413, 4059, 1625]
TOPICS += [4163, 3336, 6027, 1611]
# Note: the 35 past-exam items (D-74), 26k chars together.
ITEMS = [210, 1017, 1017, 508, 578, 459, 961, 817, 762, 765, 202, 388, 691, 295, 921, 830, 849]
ITEMS += [1087, 499, 503, 650, 972, 977, 864, 486, 541, 1064, 1337, 860, 848, 733, 670, 972]
ITEMS += [985, 984]


def _within_budget(parts: list[Part], head: int, pages, extras) -> None:
    """Every piece once, in order, and every part under budget unless it holds one piece alone."""
    assert [i for p in parts for i in p.pages] == list(range(len(pages)))
    assert [i for p in parts for i in p.extras] == list(range(len(extras)))
    for n, p in enumerate(parts):
        chars = (head if n == 0 else 0) + sum(pages[i][0] for i in p.pages)
        chars += sum(extras[i] for i in p.extras)
        size = sum(pages[i][1] for i in p.pages)
        if len(p.pages) + len(p.extras) > 1:
            assert chars <= MAX_CHARS and size <= MAX_BYTES, f"part {n + 1} over budget"


# --- the real deck ---------------------------------------------------------------------------


def test_tagging_the_deck_takes_four_parts():
    # Pages are held back by bytes (4 pictures ~ 546 KB), topics by chars. The 18k topic after
    # topic 2 does not fit part 1's chars left, so it waits - and the last two spill into part 4.
    parts = split(1000, DECK, TOPICS)
    assert parts == [
        Part(pages=[0, 1, 2, 3], extras=[0, 1, 2]),
        Part(pages=[4, 5, 6, 7], extras=[3, 4, 5, 6, 7]),
        Part(pages=[8, 9], extras=[8, 9, 10, 11, 12, 13, 14, 15]),
        Part(pages=[], extras=[16, 17]),
    ]
    _within_budget(parts, 1000, DECK, TOPICS)


def test_the_note_on_the_deck_is_held_back_by_pictures_alone():
    # 26k of items fit part 1 whole; the pictures still need three parts.
    parts = split(3000, DECK, ITEMS)
    assert parts == [
        Part(pages=[0, 1, 2, 3], extras=list(range(35))),
        Part(pages=[4, 5, 6, 7], extras=[]),
        Part(pages=[8, 9], extras=[]),
    ]
    _within_budget(parts, 3000, DECK, ITEMS)


def test_a_text_paper_with_one_picture_is_one_part():
    pages = [(2400, 0), (2100, 152000), (1800, 0), (900, 0)]
    assert split(2000, pages, []) == [Part(pages=[0, 1, 2, 3], extras=[])]


# --- edges -----------------------------------------------------------------------------------


def test_nothing_to_send_is_one_empty_part():
    assert split(1000, [], []) == [Part()]


def test_extras_left_when_the_pages_run_out_get_parts_of_their_own():
    assert split(0, [(500, 100_000)], [30_000, 30_000]) == [
        Part(pages=[0], extras=[0]),
        Part(pages=[], extras=[1]),
    ]


def test_an_entry_over_budget_goes_alone_and_keeps_its_place():
    # Never cut, never refused (D-145 item 7); the entry after it waits for its own part.
    assert split(0, [], [10_000, 60_000, 10_000]) == [
        Part(extras=[0]),
        Part(extras=[1]),
        Part(extras=[2]),
    ]


def test_a_picture_over_budget_goes_alone():
    # A scanned page could be this big.
    pages = [(500, 100_000), (500, 700_000), (500, 100_000)]
    assert split(0, pages, []) == [Part(pages=[0]), Part(pages=[1]), Part(pages=[2])]


def test_the_head_counts_against_part_one():
    # 49k of rules leave 1k: the 2k entry fits an empty part, so it waits for part 2 rather
    # than going alone into part 1 over budget.
    assert split(49_000, [], [2000]) == [Part(), Part(extras=[0])]


def test_a_page_that_does_not_fit_starts_the_next_part_before_any_entry_does():
    # Pages first in every part: part 2 opens with page 1, then the entry fills its chars.
    pages = [(1000, 500_000), (1000, 500_000)]
    assert split(0, pages, [5000]) == [
        Part(pages=[0], extras=[0]),
        Part(pages=[1], extras=[]),
    ]


def test_an_entry_over_budget_first_takes_part_one_with_no_empty_part_before_it():
    assert split(0, [], [60_000]) == [Part(extras=[0])]
