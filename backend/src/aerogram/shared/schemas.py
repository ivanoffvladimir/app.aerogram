"""DTO, общие для нескольких модулей API.

Здесь живут только те схемы, которые встречаются в контракте больше чем
в одном модуле. Схема одного модуля остаётся в его ``schemas.py``.
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field

from aerogram.shared.money import Money

__all__ = ["MoneySchema"]

CurrencyCode = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")]


class MoneySchema(BaseModel):
    """Денежная величина в контракте API.

    Соответствует схеме ``Money`` из ``docs/tz/v3/openapi.yaml``:
    целое число минорных единиц и код валюты. Дробных денег в контракте нет —
    ``amount_minor`` сериализуется как целое.
    """

    model_config = ConfigDict(frozen=True)

    amount_minor: int
    currency: CurrencyCode

    @classmethod
    def of(cls, money: Money) -> Self:
        """Из доменного типа в схему."""
        return cls(amount_minor=money.amount_minor, currency=money.currency)

    def to_money(self) -> Money:
        """Из схемы в доменный тип. Здесь же валидируется код валюты."""
        return Money(self.amount_minor, self.currency)
