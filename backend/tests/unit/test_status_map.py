"""Нормализация статусов перевозчиков (раздел 9 ТЗ)."""

from __future__ import annotations

import pytest

from aerogram.carriers.status_map import load_status_map, normalize_status
from aerogram.shared.enums import FINAL_STATUSES, ShipmentStatus


class TestCdekMap:
    def test_maps_delivered(self) -> None:
        status, unmapped = normalize_status("cdek", "DELIVERED")
        assert status is ShipmentStatus.DELIVERED
        assert unmapped is False

    def test_is_case_insensitive(self) -> None:
        assert normalize_status("cdek", "delivered")[0] is ShipmentStatus.DELIVERED

    def test_ignores_surrounding_whitespace(self) -> None:
        assert normalize_status("cdek", "  DELIVERED  ")[0] is ShipmentStatus.DELIVERED

    def test_unknown_status_falls_back_to_in_transit_and_is_flagged(self) -> None:
        # Несопоставленный статус не роняет обработку события: он получает IN_TRANSIT
        # и попадает в очередь ручного сопоставления (раздел 9 ТЗ).
        status, unmapped = normalize_status("cdek", "СОВЕРШЕННО_НОВЫЙ_СТАТУС")
        assert status is ShipmentStatus.IN_TRANSIT
        assert unmapped is True

    def test_no_status_is_mapped_twice(self) -> None:
        # Двойное сопоставление означало бы недетерминированный статус отправления.
        # Проверка встроена в загрузчик; здесь фиксируем, что карта СДЭК ей удовлетворяет.
        assert load_status_map("cdek").by_code


class TestMapLoading:
    def test_missing_map_reports_clearly(self) -> None:
        with pytest.raises(FileNotFoundError, match="карты статусов"):
            load_status_map("несуществующий_перевозчик")


class TestFinalStatuses:
    def test_final_set_matches_specification(self) -> None:
        assert {
            ShipmentStatus.DELIVERED,
            ShipmentStatus.RETURNED,
            ShipmentStatus.CANCELLED,
        } == FINAL_STATUSES

    def test_cdek_map_covers_every_final_status(self) -> None:
        # Если финальный статус не сопоставлен, polling не остановится никогда.
        mapped = set(load_status_map("cdek").by_code.values())
        assert mapped >= FINAL_STATUSES
