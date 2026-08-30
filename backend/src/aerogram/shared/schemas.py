"""DTO, общие для нескольких модулей API.

Здесь живут только те схемы, которые встречаются в контракте больше чем
в одном модуле. Схема одного модуля остаётся в его ``schemas.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field

from aerogram.shared.money import Money

__all__ = ["AddressSchema", "MoneySchema", "PackageSchema"]

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


class AddressSchema(BaseModel):
    """Адрес в контракте API (схема ``Address`` из openapi.yaml).

    Идентификатора ФИАС здесь нет намеренно: клиент присылает адрес так, как он
    хранится у него в ERP. Разрешение города до кода перевозчика — наша забота
    (системное ТЗ, раздел 7), и делается она в ``directories``.

    ``address_line`` содержит улицу и дом, то есть персональные данные:
    в логи и трассировки не попадает (CLAUDE.md §6).
    """

    country: str = Field(min_length=2, max_length=2, default="RU")
    region: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=255)
    postal_code: str | None = Field(default=None, max_length=10)
    address_line: str = Field(min_length=1, max_length=500)


class PackageSchema(BaseModel):
    """Грузовое место (схема ``Package`` из openapi.yaml).

    Единицы — граммы и миллиметры, целыми числами. Дробный вес в килограммах
    на границе API означал бы те же проблемы округления, что и дробные деньги.
    """

    weight_grams: int = Field(gt=0, le=1_000_000)
    length_mm: int | None = Field(default=None, gt=0, le=10_000)
    width_mm: int | None = Field(default=None, gt=0, le=10_000)
    height_mm: int | None = Field(default=None, gt=0, le=10_000)

    @property
    def weight_kg(self) -> Decimal:
        """Вес в килограммах для адаптеров, которые считают в них."""
        return Decimal(self.weight_grams) / Decimal(1000)
