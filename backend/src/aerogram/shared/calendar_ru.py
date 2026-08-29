"""Производственный календарь РФ.

Фактический срок доставки считается в **рабочих** днях (FR-6.2), иначе новогодние
каникулы превращают любого перевозчика в нарушителя SLA, а Carrier Score — в мусор.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import yaml

__all__ = ["ProductionCalendar", "load_calendar"]

_DATA_FILE = Path(__file__).parent / "data" / "production_calendar_ru.yaml"
_SATURDAY = 5


@dataclass(frozen=True, slots=True)
class ProductionCalendar:
    """Календарь рабочих дней РФ.

    ``base_holidays`` — нерабочие праздничные дни по ТК РФ, действуют в любой год.
    ``holidays`` и ``working_weekends`` — переносы конкретного года из постановления.
    """

    base_holidays: frozenset[tuple[int, int]]
    holidays: frozenset[date]
    working_weekends: frozenset[date]
    verified_years: frozenset[int]

    def is_verified(self, year: int) -> bool:
        """Сверен ли год с официальным постановлением о переносах."""
        return year in self.verified_years

    def is_working_day(self, day: date) -> bool:
        """Рабочий ли день с учётом праздников и переносов."""
        if day in self.working_weekends:
            return True
        if day in self.holidays:
            return False
        if (day.month, day.day) in self.base_holidays:
            return False
        return day.weekday() < _SATURDAY

    def business_days_between(self, start: date, end: date) -> int:
        """Число рабочих дней между двумя датами.

        Считается полуинтервалом ``(start, end]``: день приёма груза не входит,
        день доставки входит. Это и есть «срок в днях» в терминах перевозчиков.
        Если ``end`` раньше ``start``, результат отрицательный.
        """
        if end == start:
            return 0
        sign = 1 if end > start else -1
        lo, hi = (start, end) if sign == 1 else (end, start)

        days = 0
        cursor = lo + timedelta(days=1)
        while cursor <= hi:
            if self.is_working_day(cursor):
                days += 1
            cursor += timedelta(days=1)
        return days * sign

    def add_business_days(self, start: date, days: int) -> date:
        """Прибавить к дате заданное число рабочих дней."""
        if days < 0:
            raise ValueError("число рабочих дней не может быть отрицательным")
        cursor = start
        remaining = days
        while remaining > 0:
            cursor += timedelta(days=1)
            if self.is_working_day(cursor):
                remaining -= 1
        return cursor


@lru_cache(maxsize=1)
def load_calendar() -> ProductionCalendar:
    """Загрузить календарь из YAML. Кэшируется на процесс."""
    raw = cast(dict[str, Any], yaml.safe_load(_DATA_FILE.read_text(encoding="utf-8")))

    base: set[tuple[int, int]] = set()
    for item in raw["base_holidays"]:
        month, day = item.split("-")
        base.add((int(month), int(day)))

    holidays: set[date] = set()
    working_weekends: set[date] = set()
    verified: set[int] = set()

    for year, cfg in (raw.get("years") or {}).items():
        if cfg.get("verified"):
            verified.add(int(year))
        holidays.update(date.fromisoformat(d) for d in (cfg.get("holidays") or []))
        working_weekends.update(date.fromisoformat(d) for d in (cfg.get("working_weekends") or []))

    return ProductionCalendar(
        base_holidays=frozenset(base),
        holidays=frozenset(holidays),
        working_weekends=frozenset(working_weekends),
        verified_years=frozenset(verified),
    )
