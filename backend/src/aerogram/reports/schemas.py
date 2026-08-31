"""DTO сводки кабинета."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from aerogram.shared.schemas import MoneySchema

__all__ = [
    "CostsOut",
    "DeliveryOut",
    "OverridesOut",
    "SummaryOut",
]


class DeliveryOut(BaseModel):
    """Соблюдение срока.

    ``on_time_rate`` считается только по доставкам, у которых дедлайн был:
    доставка без срока не может ни уложиться, ни опоздать, и включать её
    в знаменатель значило бы разбавлять долю теми, кого никто не мерил.
    ``None`` означает «мерить было нечего», а не «ноль процентов».
    """

    delivered: int
    with_deadline: int
    on_time: int
    late: int
    on_time_rate: float | None
    #: Средняя просрочка по опоздавшим, а не по всем: усреднение по всем
    #: превращает редкое тяжёлое опоздание в незаметные минуты.
    average_delay_hours: float | None
    max_delay_hours: float | None
    damaged: int
    claims: int


class CostsOut(BaseModel):
    """Расходы в одной валюте. Разные валюты не складываются (CLAUDE.md §6)."""

    currency: str
    shipments: int
    quoted: MoneySchema
    actual: MoneySchema
    #: По скольким отправлениям счёт уже пришёл. Без этого числа сумма факта
    #: выглядит как экономия, хотя это просто ещё не выставленные счета.
    with_actual: int


class OverridesOut(BaseModel):
    """Решения за период и доля отказов от рекомендации."""

    decisions: int
    overrides: int
    auto: int
    override_rate: float | None
    by_reason: dict[str, int]


class SummaryOut(BaseModel):
    """Сводка кабинета за период.

    ``exceptions`` — состояние на сейчас, а не за период: разбирать нужно то,
    что горит сегодня. Остальные разделы считаются за окно ``days``.
    """

    days: int
    since: datetime
    delivery: DeliveryOut
    costs: list[CostsOut]
    overrides: OverridesOut
    exceptions: dict[str, int]
    exceptions_total: int
