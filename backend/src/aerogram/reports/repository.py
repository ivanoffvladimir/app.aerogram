"""Репозиторий сводки. Единственное место с SQL в модуле.

Все агрегаты считаются в базе, а не в Python: выгружать отправления тенанта
в память ради одной суммы значит расти вместе с клиентом и однажды перестать
открываться. Тенант в условиях не указывается — его обеспечивает RLS.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.routing.models import Decision
from aerogram.shipments.models import Shipment
from aerogram.tracking.models import DeliveryOutcome

__all__ = ["CostRow", "DeliveryStats", "OverrideStats", "ReportRepository"]


@dataclass(frozen=True, slots=True)
class DeliveryStats:
    """Соблюдение срока по доставленным за период."""

    delivered: int
    #: Доставки, у которых дедлайн вообще был. Только они участвуют в доле
    #: «в срок»: доставка без дедлайна не может ни уложиться, ни опоздать.
    with_deadline: int
    on_time: int
    late: int
    total_delay_seconds: int
    max_delay_seconds: int
    damaged: int
    claims: int


@dataclass(frozen=True, slots=True)
class CostRow:
    """Расходы в одной валюте. Складывать разные валюты запрещено (CLAUDE.md §6)."""

    currency: str
    shipments: int
    quoted_minor: int
    #: Фактическая сумма приходит из счёта и появляется позже доставки,
    #: поэтому считается по своему числу отправлений, а не по общему.
    actual_minor: int
    with_actual: int


@dataclass(frozen=True, slots=True)
class OverrideStats:
    """Решения за период: сколько раз рекомендацию не приняли."""

    decisions: int
    overrides: int
    auto: int
    by_reason: dict[str, int]


class ReportRepository:
    """Сводка тенанта. Только чтение."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def delivery_stats(self, since: datetime) -> DeliveryStats:
        """Итоги доставок, завершённых начиная с ``since``."""
        stmt = select(
            func.count(),
            func.count().filter(DeliveryOutcome.deadline_met.is_not(None)),
            func.count().filter(DeliveryOutcome.deadline_met.is_(True)),
            func.count().filter(DeliveryOutcome.deadline_met.is_(False)),
            func.coalesce(func.sum(DeliveryOutcome.delay_seconds), 0),
            func.coalesce(func.max(DeliveryOutcome.delay_seconds), 0),
            func.count().filter(DeliveryOutcome.damage.is_(True)),
            func.count().filter(DeliveryOutcome.claim.is_(True)),
        ).where(
            DeliveryOutcome.delivered_at.is_not(None),
            DeliveryOutcome.delivered_at >= since,
        )
        row = (await self._session.execute(stmt)).one()
        return DeliveryStats(
            delivered=row[0],
            with_deadline=row[1],
            on_time=row[2],
            late=row[3],
            total_delay_seconds=int(row[4]),
            max_delay_seconds=int(row[5]),
            damaged=row[6],
            claims=row[7],
        )

    async def costs(self, since: datetime) -> list[CostRow]:
        """Расходы по валютам за период, по дате создания отправления."""
        stmt = (
            select(
                Shipment.currency,
                func.count(),
                func.coalesce(func.sum(Shipment.price_quoted_amount_minor), 0),
                func.coalesce(func.sum(Shipment.price_actual_amount_minor), 0),
                func.count().filter(Shipment.price_actual_amount_minor.is_not(None)),
            )
            .where(Shipment.created_at >= since)
            .group_by(Shipment.currency)
            .order_by(Shipment.currency)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            CostRow(
                currency=row[0],
                shipments=row[1],
                quoted_minor=int(row[2]),
                actual_minor=int(row[3]),
                with_actual=row[4],
            )
            for row in rows
        ]

    async def override_stats(self, since: datetime) -> OverrideStats:
        """Решения и доля отказов от рекомендации (Override Rate)."""
        totals = select(
            func.count(),
            func.count().filter(Decision.override.is_(True)),
            func.count().filter(Decision.mode == "auto"),
        ).where(Decision.decided_at >= since)
        row = (await self._session.execute(totals)).one()

        reasons = (
            select(Decision.override_reason, func.count())
            .where(Decision.decided_at >= since, Decision.override_reason.is_not(None))
            .group_by(Decision.override_reason)
        )
        by_reason = {str(name): count for name, count in (await self._session.execute(reasons))}
        return OverrideStats(decisions=row[0], overrides=row[1], auto=row[2], by_reason=by_reason)
