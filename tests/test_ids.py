"""ULID primary keys (D-32): the schema's 26-char rule, and "newest first" by plain text sort."""

import time

from studysystem.services.ids import new_id


def test_an_id_is_26_chars():
    assert len(new_id()) == 26


def test_ids_made_in_the_same_millisecond_keep_their_order():
    # A tight loop makes many ids per millisecond - the case `study_add_course` hits with its
    # three rows. Without monotonic ids the random tails would put these in any order.
    ids = [new_id() for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert ids == sorted(ids)


def test_an_id_made_later_sorts_later():
    first = new_id()
    time.sleep(0.002)  # past the millisecond, so the timestamp prefix itself differs
    second = new_id()
    assert first[:10] < second[:10]  # the first 10 chars are the timestamp
    assert first < second
