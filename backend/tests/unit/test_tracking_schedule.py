"""Адаптивная частота опроса (FR-3.2).

Таблица из ТЗ проверяется по каждой строке: ошибка здесь не заметна глазом —
она проявляется тем, что статусы обновляются реже, чем обещано клиенту,
или что платформа опрашивает перевозчика вхолостую и тратит его лимит.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aerogram.shared.enums import ShipmentStatus
from aerogram.tracking.service import STALE_AFTER, next_poll_after

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class TestScheduleFollowsTheSpecTable:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (ShipmentStatus.CREATED, timedelta(hours=1)),
            (ShipmentStatus.ACCEPTED, timedelta(hours=1)),
            (ShipmentStatus.PICKED_UP, timedelta(hours=3)),
            (ShipmentStatus.IN_TRANSIT, timedelta(hours=3)),
            (ShipmentStatus.AT_DESTINATION_HUB, timedelta(minutes=30)),
            (ShipmentStatus.OUT_FOR_DELIVERY, timedelta(minutes=30)),
            (ShipmentStatus.READY_FOR_PICKUP, timedelta(minutes=30)),
        ],
    )
    def test_interval_matches_the_state(self, status: ShipmentStatus, expected: timedelta) -> None:
        when, stale = next_poll_after(status, NOW - timedelta(minutes=5), NOW)
        assert when == NOW + expected
        assert not stale

    @pytest.mark.parametrize(
        "status",
        [ShipmentStatus.DELIVERED, ShipmentStatus.RETURNED, ShipmentStatus.CANCELLED],
    )
    def test_polling_stops_at_a_final_state(self, status: ShipmentStatus) -> None:
        """Спрашивать больше нечего, а лимит перевозчика не бесконечен."""
        when, stale = next_poll_after(status, NOW - timedelta(days=30), NOW)
        assert when is None
        assert not stale


class TestStalled:
    def test_silence_longer_than_five_days_raises_the_flag(self) -> None:
        """Отсутствие событий само по себе новость."""
        when, stale = next_poll_after(
            ShipmentStatus.IN_TRANSIT, NOW - STALE_AFTER - timedelta(minutes=1), NOW
        )
        assert stale
        assert when == NOW + timedelta(days=1)

    def test_exactly_five_days_is_not_yet_stalled(self) -> None:
        """Граница включительно: ровно пять суток — ещё не «зависло»."""
        _, stale = next_poll_after(ShipmentStatus.IN_TRANSIT, NOW - STALE_AFTER, NOW)
        assert not stale

    def test_a_shipment_without_events_is_not_called_stalled(self) -> None:
        """Событий не было ни одного — это «ещё не начиналось», а не «зависло»."""
        when, stale = next_poll_after(ShipmentStatus.CREATED, None, NOW)
        assert not stale
        assert when == NOW + timedelta(hours=1)

    def test_a_final_state_is_never_stalled(self) -> None:
        """Доставленное молчит по естественной причине."""
        _, stale = next_poll_after(ShipmentStatus.DELIVERED, NOW - timedelta(days=365), NOW)
        assert not stale
