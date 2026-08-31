"""Порядок разбора исключений.

Список читают сверху вниз, поэтому его порядок — это порядок работы оператора.
Проверяются оба правила: тяжесть причины важнее давности, а отправление
без единого события считается молчащим дольше всех, а не меньше всех.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aerogram.shared.ids import uuid7
from aerogram.tracking.exceptions import (
    REASON_DEADLINE_PASSED,
    REASON_PROBLEM_STATUS,
    REASON_STALLED,
    _counters,
    _order,
)
from aerogram.tracking.schemas import ShipmentExceptionOut

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _item(reasons: list[str], last_event_at: datetime | None) -> ShipmentExceptionOut:
    return ShipmentExceptionOut(
        id=uuid7(),
        number="AG-1",
        carrier_name="СДЭК",
        tracking_number=None,
        status="InTransit",
        deadline=None,
        last_event_at=last_event_at,
        reasons=reasons,
    )


class TestOrder:
    def test_severity_beats_age(self) -> None:
        """Свежий сорванный срок разбирают раньше давно зависшего."""
        stalled = _item([REASON_STALLED], datetime(2026, 7, 1, tzinfo=UTC))
        missed = _item([REASON_DEADLINE_PASSED], NOW)

        assert sorted([stalled, missed], key=_order) == [missed, stalled]

    def test_within_one_severity_the_longest_silence_comes_first(self) -> None:
        older = _item([REASON_STALLED], datetime(2026, 7, 1, tzinfo=UTC))
        newer = _item([REASON_STALLED], datetime(2026, 8, 1, tzinfo=UTC))

        assert sorted([newer, older], key=_order) == [older, newer]

    def test_a_shipment_without_events_is_the_worst_case(self) -> None:
        """О нём не известно вообще ничего — это хуже старого события."""
        silent = _item([REASON_STALLED], None)
        old = _item([REASON_STALLED], datetime(2026, 1, 1, tzinfo=UTC))

        assert sorted([old, silent], key=_order) == [silent, old]

    def test_the_heaviest_reason_decides_for_a_row_with_several(self) -> None:
        both = _item([REASON_DEADLINE_PASSED, REASON_STALLED], NOW)
        problem = _item([REASON_PROBLEM_STATUS], NOW)

        assert sorted([problem, both], key=_order) == [both, problem]


class TestCounters:
    def test_a_row_with_two_reasons_counts_in_both(self) -> None:
        """Сумма счётчиков больше числа строк — и это верно, а не ошибка."""
        counters = _counters([_item([REASON_DEADLINE_PASSED, REASON_STALLED], NOW)])

        assert counters == {
            REASON_DEADLINE_PASSED: 1,
            REASON_PROBLEM_STATUS: 0,
            REASON_STALLED: 1,
        }

    def test_every_reason_is_present_even_at_zero(self) -> None:
        """Экран рисует счётчики по ключам ответа: пропуск ключа скрыл бы причину."""
        assert set(_counters([])) == {
            REASON_DEADLINE_PASSED,
            REASON_PROBLEM_STATUS,
            REASON_STALLED,
        }
