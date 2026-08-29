"""UUIDv7: версия, вариант и монотонность по времени."""

from __future__ import annotations

import time

import pytest

from aerogram.shared.ids import uuid7, uuid7_timestamp


def test_version_is_7() -> None:
    assert uuid7().version == 7


def test_variant_is_rfc4122() -> None:
    assert uuid7().variant == "specified in RFC 4122"


def test_values_are_unique() -> None:
    assert len({uuid7() for _ in range(1000)}) == 1000


def test_sorts_by_creation_time() -> None:
    # Ради чего и выбран UUIDv7: локальность в B-tree и хронологический порядок.
    first = uuid7()
    time.sleep(0.005)
    second = uuid7()
    assert str(first) < str(second)


def test_timestamp_matches_wall_clock() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    assert before <= uuid7_timestamp(value) <= after


def test_timestamp_rejects_other_versions() -> None:
    from uuid import uuid4

    with pytest.raises(ValueError, match="UUIDv7"):
        uuid7_timestamp(uuid4())
