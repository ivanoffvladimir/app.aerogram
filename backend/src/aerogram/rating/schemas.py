"""DTO расчёта: запрос, котировка, выдача."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aerogram.shared.enums import CargoType, PriceSource
from aerogram.shared.schemas import MoneySchema

__all__ = [
    "CargoIn",
    "OptionsIn",
    "PartyIn",
    "PlaceIn",
    "RateErrorOut",
    "RateQuoteOut",
    "RateRequestIn",
    "RateResponse",
]


class PlaceIn(BaseModel):
    """Одно грузовое место."""

    weight_kg: Decimal = Field(gt=0, le=Decimal("1000"))
    length_cm: int = Field(gt=0, le=1000)
    width_cm: int = Field(gt=0, le=1000)
    height_cm: int = Field(gt=0, le=1000)


class PartyIn(BaseModel):
    """Пункт отправления или назначения."""

    city_fias_id: str | None = Field(default=None, min_length=36, max_length=36)
    city_name: str = Field(min_length=1, max_length=255)
    postal_code: str | None = Field(default=None, max_length=10)
    address: str | None = Field(default=None, max_length=500)


class CargoIn(BaseModel):
    type: CargoType = CargoType.PARCEL
    declared_value: MoneySchema = MoneySchema(amount_minor=0, currency="RUB")


class OptionsIn(BaseModel):
    insurance: bool = False
    pickup: bool = True
    delivery_to_door: bool = True


class RateRequestIn(BaseModel):
    """Запрос расчёта (FR-1.1)."""

    sender: PartyIn
    recipient: PartyIn
    places: list[PlaceIn] = Field(min_length=1, max_length=255)
    cargo: CargoIn = Field(default_factory=CargoIn)
    options: OptionsIn = Field(default_factory=OptionsIn)
    required_delivery_date: date | None = None
    #: Ограничить расчёт этими перевозчиками. Пусто — все подключённые.
    carriers: list[str] = Field(default_factory=list)


class RateQuoteOut(BaseModel):
    """Строка выдачи."""

    model_config = ConfigDict(from_attributes=True)

    rate_id: UUID
    carrier: str
    service_code: str | None = None
    tariff_code: str | None = None
    service_name: str | None = None
    price: MoneySchema
    price_source: PriceSource | None = None
    transit_days_min: int | None = None
    transit_days_max: int | None = None
    promised_delivery_date: date | None = None
    meets_deadline: bool | None = None
    rank: int | None = None


class RateErrorOut(BaseModel):
    """Перевозчик, не давший котировку (FR-1.4).

    Отдельная строка выдачи, а не молчание: пользователь должен видеть,
    что перевозчик опрошен и почему не ответил.
    """

    carrier: str
    code: str
    message: str


class RateResponse(BaseModel):
    """Выдача расчёта."""

    request_id: UUID
    expires_at: datetime
    duration_ms: int
    quotes: list[RateQuoteOut]
    errors: list[RateErrorOut]
