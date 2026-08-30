"""DTO расчёта. Соответствуют схемам из ``docs/tz/v3/openapi.yaml``.

Расхождение с этим файлом есть ошибка кода, а не повод поправить контракт:
по нему фронт генерирует типизированный клиент.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aerogram.shared.enums import (
    CargoType,
    CostComponentType,
    IneligibilityReason,
    OfferSource,
    ProbabilityLabel,
    RiskLevel,
    RoutingStrategy,
    ScoreConfidence,
)
from aerogram.shared.schemas import AddressSchema, MoneySchema, PackageSchema

__all__ = [
    "CarrierFailureOut",
    "CostComponentOut",
    "RateOfferOut",
    "RateRequestIn",
    "RateResponse",
]

#: Дополнительные услуги, которые понимает расчёт. Неизвестная услуга
#: не игнорируется молча — запрос отклоняется с указанием поля.
KNOWN_SERVICES = frozenset({"insurance", "pickup", "door_delivery"})


class RateRequestIn(BaseModel):
    """Запрос расчёта (схема ``RateRequest``)."""

    origin: AddressSchema
    destination: AddressSchema
    ship_at: datetime | None = None
    #: Жёсткое ограничение для активной рекомендации: дата или дата со временем.
    deadline: datetime | None = None
    packages: list[PackageSchema] = Field(min_length=1, max_length=255)
    cargo_value: MoneySchema
    cargo_type: CargoType = CargoType.PARCEL
    additional_services: list[str] = Field(default_factory=list)
    carrier_whitelist: list[UUID] = Field(default_factory=list)
    carrier_blacklist: list[UUID] = Field(default_factory=list)
    strategy: RoutingStrategy = RoutingStrategy.OPTIMAL

    @field_validator("ship_at", "deadline")
    @classmethod
    def _must_carry_a_timezone(cls, value: datetime | None) -> datetime | None:
        """Момент без зоны отвергается, а не достраивается.

        Достроить зону значило бы выбрать её за клиента: для отправления
        Москва → Владивосток разница в семь часов решает, уложился ли
        перевозчик в срок. Схема контракта требует ``date-time``, а он
        содержит смещение.
        """
        if value is not None and value.tzinfo is None:
            raise ValueError("укажите часовой пояс: например, 2026-09-05T12:00:00+03:00")
        return value

    @field_validator("additional_services")
    @classmethod
    def _known_services_only(cls, value: list[str]) -> list[str]:
        """Неизвестная услуга отклоняется, а не игнорируется молча.

        Молчаливое игнорирование дало бы расчёт без страхования по запросу,
        в котором страхование просили, — и заметили бы это при страховом случае.
        """
        unknown = sorted(set(value) - KNOWN_SERVICES)
        if unknown:
            raise ValueError("неизвестные дополнительные услуги: " + ", ".join(unknown))
        return value

    # Пустой список означает отсутствие доплат, то есть склад-склад без
    # страхования, — так этот список читается в примере ТЗ
    # (docs/tz/v3/json-examples/01_rate_request.json).

    @property
    def insurance(self) -> bool:
        return "insurance" in self.additional_services

    @property
    def pickup(self) -> bool:
        """Забор у отправителя, а не сдача на склад."""
        return "pickup" in self.additional_services

    @property
    def delivery_to_door(self) -> bool:
        return "door_delivery" in self.additional_services


class CostComponentOut(BaseModel):
    """Составляющая стоимости (схема ``CostComponent``)."""

    model_config = ConfigDict(from_attributes=True)

    type: CostComponentType
    money: MoneySchema
    #: Ставка в процентах: 0.18 означает 0.18 %.
    rate_percent: Decimal | None = None
    description: str | None = None


class RateOfferOut(BaseModel):
    """Одно предложение в выдаче (схема ``RateOffer``)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    carrier_id: UUID
    carrier_name: str | None = None
    service_code: str
    service_name: str | None = None
    source: OfferSource | None = None
    cost_components: list[CostComponentOut] = Field(default_factory=list)
    total_cost: MoneySchema
    eta: datetime | None = None
    #: Запас до дедлайна и величина опоздания — в секундах, а не в днях:
    #: дедлайн может быть задан со временем.
    deadline_margin_seconds: int | None = None
    lateness_seconds: int | None = None
    on_time_probability: Decimal | None = Field(default=None, ge=0, le=1)
    probability_label: ProbabilityLabel | None = None
    carrier_score: Decimal | None = Field(default=None, ge=0, le=100)
    risk: RiskLevel | None = None
    confidence: ScoreConfidence | None = None
    eligible: bool
    ineligibility_reason: IneligibilityReason | None = None
    valid_until: datetime | None = None


class CarrierFailureOut(BaseModel):
    """Перевозчик, не вернувший расчёт (схема ``CarrierFailure``).

    Не ошибка запроса: partial success — нормальное состояние выдачи
    (системное ТЗ, раздел 8).
    """

    carrier_id: UUID | None = None
    carrier_code: str | None = None
    code: str
    message: str
    retryable: bool = False


class RateResponse(BaseModel):
    """Выдача расчёта (схема ``RateResponse``)."""

    quote_id: UUID
    offers: list[RateOfferOut]
    failures: list[CarrierFailureOut]
    #: true — в срок не укладывается ни одно предложение. Отдельный признак,
    #: а не пустая выдача: альтернативы всё равно показываются.
    no_deadline_match: bool
    valid_until: datetime
