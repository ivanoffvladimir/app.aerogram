"""DTO сверки расходов: строка, итог по валюте, итог по перевозчику."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel

from aerogram.shared.schemas import MoneySchema

__all__ = [
    "CarrierTotalsOut",
    "CostLineOut",
    "CurrencyTotalsOut",
    "ReconciliationOut",
    "ReconciliationState",
]


class ReconciliationState(StrEnum):
    """Состояние сверки одного отправления.

    Пять значений, а не «сошлось / не сошлось»: три из них означают, что
    сравнивать нечего, и сваливать их в «сошлось» — прямая неправда
    о деньгах. Экран, показывающий «расхождений нет» там, где счетов
    не приходило вовсе, хуже отсутствия экрана: по нему принимают решение
    не проверять перевозчика.
    """

    #: Счёт ещё не пришёл: факт не заполнен. Ожидаемое состояние сразу
    #: после оформления, а не проблема.
    AWAITING = "awaiting"
    #: Факт есть, котировки нет — сравнивать не с чем. Так бывает
    #: у отправлений, созданных в обход расчёта.
    NO_QUOTE = "no_quote"
    #: Совпало до копейки.
    MATCHED = "matched"
    #: Перевозчик выставил больше, чем обещал расчёт.
    OVERCHARGED = "overcharged"
    #: Меньше. Тоже расхождение: занижение говорит либо об ошибке счёта,
    #: либо о том, что мы завышали котировку и проигрывали сравнение.
    UNDERCHARGED = "undercharged"


class CostLineOut(BaseModel):
    """Одно отправление в сверке.

    ``difference`` — «факт минус котировка», поэтому положительное число
    означает перерасход. Считается только когда есть оба числа: разность
    с пустотой — это не ноль, это отсутствие ответа.
    """

    shipment_id: UUID
    number: str
    created_at: datetime
    carrier_id: UUID | None
    carrier_name: str | None
    status: str
    state: ReconciliationState
    quoted: MoneySchema | None
    actual: MoneySchema | None
    difference: MoneySchema | None
    #: Расхождение в процентах от котировки. ``None``, когда котировка ноль
    #: или её нет: делить на ноль нельзя, а показать «0 %» значило бы сказать
    #: «сошлось» про случай, где сравнивать не с чем.
    difference_percent: float | None


class CurrencyTotalsOut(BaseModel):
    """Итог по одной валюте. Разные валюты не складываются (CLAUDE.md §6).

    Разность считается **только по отправлениям, у которых есть счёт**,
    и `quoted_reconciled` — котировка именно этих отправлений. Сравнить
    сумму всех котировок с суммой пришедших счетов значило бы вычесть одно
    множество из другого и назвать разницу экономией.
    """

    currency: str
    shipments: int
    quoted: MoneySchema
    #: Котировка тех отправлений, по которым счёт уже есть.
    quoted_reconciled: MoneySchema
    actual: MoneySchema
    difference: MoneySchema
    difference_percent: float | None
    awaiting: int
    no_quote: int
    matched: int
    overcharged: int
    undercharged: int


class CarrierTotalsOut(BaseModel):
    """Итог по перевозчику в одной валюте.

    Смысл экрана: видно, чьи счета расходятся с расчётом. Строка считается
    по тем же правилам, что и итог по валюте, — только по отправлениям
    со счётом.
    """

    carrier_id: UUID | None
    carrier_name: str | None
    currency: str
    reconciled: int
    quoted: MoneySchema
    actual: MoneySchema
    difference: MoneySchema
    difference_percent: float | None


class ReconciliationOut(BaseModel):
    """Сверка расходов за период.

    ``items`` — страница списка, ``total`` — сколько строк подошло под фильтр
    целиком. Итоги считаются по всему периоду, а не по видимой странице:
    иначе сумма меняется от того, на какой странице стоит оператор.
    """

    days: int
    since: datetime
    currencies: list[CurrencyTotalsOut]
    carriers: list[CarrierTotalsOut]
    items: list[CostLineOut]
    total: int
