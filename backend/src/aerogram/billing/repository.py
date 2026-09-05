"""Репозиторий сверки расходов. Единственное место с SQL в модуле.

Агрегаты считаются в базе, а не в Python: выгружать отправления тенанта
в память ради одной суммы значит расти вместе с клиентом и однажды
перестать открываться. Тенант в условиях не указывается — его обеспечивает
RLS (CLAUDE.md §6).

**Разность считается только по отправлениям со счётом.** Сумма всех
котировок минус сумма пришедших счетов — это разность двух разных множеств,
и она тем больше, чем больше отправлений ждут счёта. Показать её как
«перерасход» или «экономию» значит соврать про деньги клиента, поэтому
котировка в итогах берётся по тем же строкам, что и факт.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.directories.models import Carrier
from aerogram.shipments.models import Shipment

__all__ = [
    "STATE_FILTERS",
    "BillingRepository",
    "CarrierTotals",
    "CostLine",
    "CurrencyTotals",
]

_QUOTED = Shipment.price_quoted_amount_minor
_ACTUAL = Shipment.price_actual_amount_minor

#: Условия отбора по состоянию сверки. Живут рядом с запросом намеренно:
#: определение состояния одно и то же для фильтра, для счётчиков в итогах
#: и для подписи строки, и разойдись они — экран показал бы одно число
#: в шапке и другое в списке.
STATE_FILTERS: dict[str, ColumnElement[bool]] = {
    "awaiting": _ACTUAL.is_(None),
    "no_quote": and_(_ACTUAL.is_not(None), _QUOTED.is_(None)),
    "matched": and_(_ACTUAL.is_not(None), _QUOTED.is_not(None), _ACTUAL == _QUOTED),
    "overcharged": and_(_ACTUAL.is_not(None), _QUOTED.is_not(None), _ACTUAL > _QUOTED),
    "undercharged": and_(_ACTUAL.is_not(None), _QUOTED.is_not(None), _ACTUAL < _QUOTED),
}

#: Отправления, которые вообще участвуют в сверке: у них есть и счёт,
#: и котировка, с которой его сравнивают.
_RECONCILED = and_(_ACTUAL.is_not(None), _QUOTED.is_not(None))


@dataclass(frozen=True, slots=True)
class CostLine:
    """Одно отправление: что обещал расчёт и что выставил перевозчик."""

    shipment_id: UUID
    number: str
    created_at: datetime
    carrier_id: UUID | None
    carrier_name: str | None
    status: str
    currency: str
    quoted_minor: int | None
    actual_minor: int | None


@dataclass(frozen=True, slots=True)
class CurrencyTotals:
    """Итог по валюте за период."""

    currency: str
    shipments: int
    quoted_minor: int
    #: Котировка ТЕХ ЖЕ отправлений, по которым пришёл счёт.
    quoted_reconciled_minor: int
    actual_minor: int
    awaiting: int
    no_quote: int
    matched: int
    overcharged: int
    undercharged: int


@dataclass(frozen=True, slots=True)
class CarrierTotals:
    """Итог по перевозчику и валюте, только по сверенным отправлениям."""

    carrier_id: UUID | None
    carrier_name: str | None
    currency: str
    reconciled: int
    quoted_minor: int
    actual_minor: int


class BillingRepository:
    """Сверка расходов тенанта. Только чтение."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _where(
        self, since: datetime, carrier_id: UUID | None, state: str | None
    ) -> list[ColumnElement[bool]]:
        """Условия отбора, общие для списка и для итогов.

        Один список на оба запроса: разойдись они, шапка считала бы
        по одному набору отправлений, а список показывал другой.
        """
        clauses: list[ColumnElement[bool]] = [Shipment.created_at >= since]
        if carrier_id is not None:
            clauses.append(Shipment.carrier_id == carrier_id)
        if state is not None:
            clauses.append(STATE_FILTERS[state])
        return clauses

    async def page(
        self,
        *,
        since: datetime,
        carrier_id: UUID | None = None,
        state: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[CostLine], int]:
        """Страница сверки и общее число подходящих отправлений."""
        clauses = self._where(since, carrier_id, state)
        total = (
            await self._session.execute(select(func.count()).select_from(Shipment).where(*clauses))
        ).scalar_one()

        stmt = (
            select(
                Shipment.id,
                Shipment.number,
                Shipment.created_at,
                Shipment.carrier_id,
                Carrier.name,
                Shipment.status,
                Shipment.currency,
                _QUOTED,
                _ACTUAL,
            )
            # Внешнее соединение: перевозчик мог быть удалён из справочника,
            # а расход остался. Внутреннее спрятало бы такие строки, и итог
            # в шапке не сошёлся бы со списком.
            .outerjoin(Carrier, Carrier.id == Shipment.carrier_id)
            .where(*clauses)
            .order_by(Shipment.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            CostLine(
                shipment_id=row[0],
                number=row[1],
                created_at=row[2],
                carrier_id=row[3],
                carrier_name=row[4],
                status=row[5],
                currency=row[6],
                quoted_minor=row[7],
                actual_minor=row[8],
            )
            for row in rows
        ], total

    async def by_currency(
        self, *, since: datetime, carrier_id: UUID | None = None, state: str | None = None
    ) -> list[CurrencyTotals]:
        """Итоги по валютам за период."""
        clauses = self._where(since, carrier_id, state)
        stmt = (
            select(
                Shipment.currency,
                func.count(),
                func.coalesce(func.sum(_QUOTED), 0),
                func.coalesce(func.sum(_QUOTED).filter(_RECONCILED), 0),
                func.coalesce(func.sum(_ACTUAL).filter(_RECONCILED), 0),
                func.count().filter(STATE_FILTERS["awaiting"]),
                func.count().filter(STATE_FILTERS["no_quote"]),
                func.count().filter(STATE_FILTERS["matched"]),
                func.count().filter(STATE_FILTERS["overcharged"]),
                func.count().filter(STATE_FILTERS["undercharged"]),
            )
            .where(*clauses)
            .group_by(Shipment.currency)
            .order_by(Shipment.currency)
        )
        return [
            CurrencyTotals(
                currency=row[0],
                shipments=row[1],
                quoted_minor=int(row[2]),
                quoted_reconciled_minor=int(row[3]),
                actual_minor=int(row[4]),
                awaiting=row[5],
                no_quote=row[6],
                matched=row[7],
                overcharged=row[8],
                undercharged=row[9],
            )
            for row in (await self._session.execute(stmt)).all()
        ]

    async def by_carrier(
        self, *, since: datetime, carrier_id: UUID | None = None, state: str | None = None
    ) -> list[CarrierTotals]:
        """Итоги по перевозчикам: чьи счета расходятся с расчётом.

        Считаются **только сверенные** отправления: перевозчик, по которому
        не пришло ни одного счёта, в разрезе не появляется вовсе. Строка
        с нулевой разницей означала бы «у него всё сходится», хотя сверять
        было нечего.
        """
        clauses = [*self._where(since, carrier_id, state), _RECONCILED]
        stmt = (
            select(
                Shipment.carrier_id,
                Carrier.name,
                Shipment.currency,
                func.count(),
                func.coalesce(func.sum(_QUOTED), 0),
                func.coalesce(func.sum(_ACTUAL), 0),
            )
            .outerjoin(Carrier, Carrier.id == Shipment.carrier_id)
            .where(*clauses)
            .group_by(Shipment.carrier_id, Carrier.name, Shipment.currency)
            .order_by(Carrier.name, Shipment.currency)
        )
        return [
            CarrierTotals(
                carrier_id=row[0],
                carrier_name=row[1],
                currency=row[2],
                reconciled=row[3],
                quoted_minor=int(row[4]),
                actual_minor=int(row[5]),
            )
            for row in (await self._session.execute(stmt)).all()
        ]
