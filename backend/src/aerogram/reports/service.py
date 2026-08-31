"""Сводка кабинета: SLA, расходы, решения, исключения (Dashboard, P1).

Модуль только читает: адаптеры отсюда не вызываются и в базу не пишется
ни строки (CLAUDE.md §4). Сводка — производная от уже собранных фактов,
и любая запись здесь означала бы, что число зависит от того, кто и когда
открыл экран.

**Экономии здесь нет намеренно.** «Сколько сэкономлено» требует базы
сравнения — самый дорогой вариант выдачи, медиана, тариф по умолчанию, —
и от выбора базы число меняется в разы. Это утверждение о деньгах клиента,
а не техническая деталь: решение человека, запись в docs/status.md.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.reports.repository import (
    CostRow,
    DeliveryStats,
    OverrideStats,
    ReportRepository,
)
from aerogram.reports.schemas import CostsOut, DeliveryOut, OverridesOut, SummaryOut
from aerogram.shared.clock import utcnow
from aerogram.shared.money import Money
from aerogram.shared.schemas import MoneySchema
from aerogram.tracking.exceptions import ExceptionService

__all__ = ["DEFAULT_DAYS", "MAX_DAYS", "ReportService"]

#: Окно сводки по умолчанию. Месяц — тот срок, за который у пилотного клиента
#: набирается достаточно доставок, чтобы доля «в срок» что-то значила.
DEFAULT_DAYS = 30
#: Год — предел. Дальше сводка перестаёт описывать сегодняшнюю работу
#: и начинает описывать историю, для которой нужен другой инструмент.
MAX_DAYS = 365

_SECONDS_IN_HOUR = 3600


class ReportService:
    """Сводка тенанта. Только чтение."""

    def __init__(self, session: AsyncSession) -> None:
        self._reports = ReportRepository(session)
        self._exceptions = ExceptionService(session)

    async def summary(self, days: int = DEFAULT_DAYS) -> SummaryOut:
        """Собрать сводку за последние ``days`` суток."""
        window = max(1, min(days, MAX_DAYS))
        since = utcnow() - timedelta(days=window)

        delivery = await self._reports.delivery_stats(since)
        costs = await self._reports.costs(since)
        overrides = await self._reports.override_stats(since)
        # Исключения — состояние на сейчас, а не за окно: разбирают то,
        # что горит сегодня, а не то, что горело месяц назад.
        open_exceptions = await self._exceptions.list_open()

        return SummaryOut(
            days=window,
            since=since,
            delivery=_delivery(delivery),
            costs=[_costs(row) for row in costs],
            overrides=_overrides(overrides),
            exceptions=open_exceptions.by_reason,
            exceptions_total=open_exceptions.total,
        )


def _delivery(stats: DeliveryStats) -> DeliveryOut:
    return DeliveryOut(
        delivered=stats.delivered,
        with_deadline=stats.with_deadline,
        on_time=stats.on_time,
        late=stats.late,
        on_time_rate=_rate(stats.on_time, stats.with_deadline),
        average_delay_hours=_hours(stats.total_delay_seconds / stats.late) if stats.late else None,
        max_delay_hours=_hours(stats.max_delay_seconds) if stats.late else None,
        damaged=stats.damaged,
        claims=stats.claims,
    )


def _costs(row: CostRow) -> CostsOut:
    return CostsOut(
        currency=row.currency,
        shipments=row.shipments,
        quoted=MoneySchema.of(Money(row.quoted_minor, row.currency)),
        actual=MoneySchema.of(Money(row.actual_minor, row.currency)),
        with_actual=row.with_actual,
    )


def _overrides(stats: OverrideStats) -> OverridesOut:
    return OverridesOut(
        decisions=stats.decisions,
        overrides=stats.overrides,
        auto=stats.auto,
        override_rate=_rate(stats.overrides, stats.decisions),
        by_reason=stats.by_reason,
    )


def _rate(part: int, whole: int) -> float | None:
    """Доля в процентах или ``None``, когда делить не на что.

    Ноль вместо ``None`` читался бы как «ни одного вовремя», хотя означал бы
    «не было ни одной подходящей доставки». Разница дорогая: по первому числу
    начинают менять перевозчика.
    """
    if whole == 0:
        return None
    return round(part * 100 / whole, 1)


def _hours(seconds: float) -> float:
    return round(seconds / _SECONDS_IN_HOUR, 1)
