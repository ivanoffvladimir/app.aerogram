"""Billing Lite: сверка «сколько обещал расчёт» и «сколько выставил счёт».

Экран `/invoices` фронт-ТЗ (раздел 2, P2) и шаг **Settle** цикла Decision
Engine. Модуль только читает: адаптеры отсюда не вызываются и в базу
не пишется ни строки (CLAUDE.md §4).

Три правила, без которых экран врал бы про деньги.

**Разность считается по одним и тем же отправлениям.** Сумма всех
котировок минус сумма пришедших счетов — разность двух разных множеств,
и она тем больше, чем больше отправлений ещё ждут счёта. Поэтому в итогах
рядом с фактом стоит котировка **тех же** строк, а полная сумма котировок
показана отдельно как план.

**«Счёта нет» — не «сошлось».** Отправление без факта не попадает ни
в перерасход, ни в экономию: оно ждёт счёта, и это отдельное состояние.
Экран, показывающий «расхождений нет» там, где счетов не приходило вовсе,
хуже отсутствия экрана — по нему решают не проверять перевозчика.

**Валюты не складываются** (CLAUDE.md §6). Итог — список по валютам,
а не одно число.

Отдельно стоит сказать, откуда вообще берётся факт. Сегодня он приходит
единственным путём: `ShipmentResult.price_actual`, то есть из ответа
перевозчика при оформлении, и его заполняют не все — из пяти адаптеров
только Деловые Линии. Импорта счетов (файл от перевозчика, сопоставление
строк с отправлениями) нет: он требует своей таблицы, то есть схемы БД
и миграции, а это построчное ревью человека (CLAUDE.md §7, пункт 1),
и формата счёта у каждого перевозчика своего. Пока его нет, экран честно
показывает «ожидает счёта» и не выдаёт пустоту за совпадение.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.billing.repository import (
    STATE_FILTERS,
    BillingRepository,
    CarrierTotals,
    CostLine,
    CurrencyTotals,
)
from aerogram.billing.schemas import (
    CarrierTotalsOut,
    CostLineOut,
    CurrencyTotalsOut,
    ReconciliationOut,
    ReconciliationState,
)
from aerogram.shared.clock import utcnow
from aerogram.shared.money import Money
from aerogram.shared.schemas import MoneySchema
from aerogram.shipments.schemas import contract_status

__all__ = ["DEFAULT_DAYS", "MAX_DAYS", "STATES", "BillingService"]

#: Окно сверки по умолчанию. Месяц — обычный расчётный период у перевозчика:
#: счета за него уже выставлены, и сверять есть что.
DEFAULT_DAYS = 30
#: Год — предел, как и в сводке кабинета: дальше экран описывает историю,
#: а не текущие расчёты.
MAX_DAYS = 365

#: Состояния, по которым разрешено фильтровать. Берутся из репозитория,
#: чтобы список фильтра не разошёлся с условиями отбора.
STATES: frozenset[str] = frozenset(STATE_FILTERS)


class BillingService:
    """Сверка расходов тенанта. Только чтение."""

    def __init__(self, session: AsyncSession) -> None:
        self._billing = BillingRepository(session)

    async def reconciliation(
        self,
        *,
        days: int = DEFAULT_DAYS,
        carrier_id: UUID | None = None,
        state: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> ReconciliationOut:
        """Сверка за последние ``days`` суток.

        Итоги считаются по всему периоду, а не по видимой странице: иначе
        сумма менялась бы от того, на какой странице стоит оператор.
        """
        window = max(1, min(days, MAX_DAYS))
        since = utcnow() - timedelta(days=window)
        lines, total = await self._billing.page(
            since=since,
            carrier_id=carrier_id,
            state=state,
            page=page,
            page_size=page_size,
        )
        currencies = await self._billing.by_currency(
            since=since, carrier_id=carrier_id, state=state
        )
        carriers = await self._billing.by_carrier(since=since, carrier_id=carrier_id, state=state)

        return ReconciliationOut(
            days=window,
            since=since,
            currencies=[_currency(row) for row in currencies],
            carriers=[_carrier(row) for row in carriers],
            items=[_line(row) for row in lines],
            total=total,
        )


def state_of(quoted_minor: int | None, actual_minor: int | None) -> ReconciliationState:
    """Состояние сверки одной строки.

    Повторяет условия ``STATE_FILTERS``, но на значениях, а не в SQL:
    одно и то же правило нужно и базе, чтобы отобрать строки, и Python,
    чтобы подписать строку. Совпадение проверяется тестом — разойдись они,
    фильтр «перерасход» показывал бы строки с подписью «сошлось».
    """
    if actual_minor is None:
        return ReconciliationState.AWAITING
    if quoted_minor is None:
        return ReconciliationState.NO_QUOTE
    if actual_minor == quoted_minor:
        return ReconciliationState.MATCHED
    if actual_minor > quoted_minor:
        return ReconciliationState.OVERCHARGED
    return ReconciliationState.UNDERCHARGED


def _line(row: CostLine) -> CostLineOut:
    quoted = _money(row.quoted_minor, row.currency)
    actual = _money(row.actual_minor, row.currency)
    difference = (
        Money(row.actual_minor, row.currency) - Money(row.quoted_minor, row.currency)
        if row.actual_minor is not None and row.quoted_minor is not None
        else None
    )
    return CostLineOut(
        shipment_id=row.shipment_id,
        number=row.number,
        created_at=row.created_at,
        carrier_id=row.carrier_id,
        carrier_name=row.carrier_name,
        # Статус — в терминах контракта, как во всём остальном API:
        # внутреннее значение здесь показало бы кабинету другое слово,
        # чем карточка того же отправления.
        status=contract_status(row.status),
        state=state_of(row.quoted_minor, row.actual_minor),
        quoted=quoted,
        actual=actual,
        difference=MoneySchema.of(difference) if difference is not None else None,
        difference_percent=_percent(
            difference.amount_minor if difference is not None else None, row.quoted_minor
        ),
    )


def _currency(row: CurrencyTotals) -> CurrencyTotalsOut:
    difference = row.actual_minor - row.quoted_reconciled_minor
    return CurrencyTotalsOut(
        currency=row.currency,
        shipments=row.shipments,
        quoted=MoneySchema.of(Money(row.quoted_minor, row.currency)),
        quoted_reconciled=MoneySchema.of(Money(row.quoted_reconciled_minor, row.currency)),
        actual=MoneySchema.of(Money(row.actual_minor, row.currency)),
        difference=MoneySchema.of(Money(difference, row.currency)),
        difference_percent=_percent(difference, row.quoted_reconciled_minor),
        awaiting=row.awaiting,
        no_quote=row.no_quote,
        matched=row.matched,
        overcharged=row.overcharged,
        undercharged=row.undercharged,
    )


def _carrier(row: CarrierTotals) -> CarrierTotalsOut:
    difference = row.actual_minor - row.quoted_minor
    return CarrierTotalsOut(
        carrier_id=row.carrier_id,
        carrier_name=row.carrier_name,
        currency=row.currency,
        reconciled=row.reconciled,
        quoted=MoneySchema.of(Money(row.quoted_minor, row.currency)),
        actual=MoneySchema.of(Money(row.actual_minor, row.currency)),
        difference=MoneySchema.of(Money(difference, row.currency)),
        difference_percent=_percent(difference, row.quoted_minor),
    )


def _money(amount_minor: int | None, currency: str) -> MoneySchema | None:
    if amount_minor is None:
        return None
    return MoneySchema.of(Money(amount_minor, currency))


def _percent(difference_minor: int | None, base_minor: int | None) -> float | None:
    """Расхождение в процентах от котировки.

    ``None``, когда базы нет или она ноль. Ноль вместо ``None`` читался бы
    как «сошлось», а означал бы «сравнивать не с чем» — разница дорогая:
    по первому числу перевозчика не проверяют.
    """
    if difference_minor is None or not base_minor:
        return None
    return round(difference_minor * 100 / base_minor, 1)
