"""DTO массовых отправлений (ADR-0022).

Строка списка хранит **получателя и груз**, а отправитель общий для прогона.
Так устроен и сам сценарий: «один отправитель, много получателей».

Запрос расчёта собирается из общего отправителя и построчного получателя
уже внутри сервиса — отдельного типа для него здесь нет намеренно, иначе
пришлось бы дублировать валидацию ``RateRequestIn``, которая и так работает.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aerogram.shared.enums import BulkRowStatus, BulkRunStatus, CargoType, RoutingStrategy
from aerogram.shared.schemas import AddressSchema, MoneySchema, PackageSchema

__all__ = [
    "BulkRowIn",
    "BulkRowOut",
    "BulkRunCreateIn",
    "BulkRunOut",
    "BulkRunPage",
    "BulkRunRenameIn",
]


class BulkRowIn(BaseModel):
    """Одна строка списка: получатель и его груз."""

    destination: AddressSchema
    packages: list[PackageSchema] = Field(min_length=1, max_length=255)
    cargo_value: MoneySchema
    cargo_type: CargoType = CargoType.PARCEL
    #: Срок для этой строки. У разных получателей он может быть разным.
    deadline: datetime | None = None


class BulkRunCreateIn(BaseModel):
    """Создание массового расчёта."""

    #: Пусто — имя задаётся по дате, как в кабинете.
    name: str | None = Field(default=None, max_length=120)
    origin: AddressSchema
    strategy: RoutingStrategy = RoutingStrategy.OPTIMAL
    rows: list[BulkRowIn] = Field(min_length=1, max_length=1000)


class BulkRunRenameIn(BaseModel):
    """Переименование прогона."""

    name: str = Field(min_length=1, max_length=120)


class BulkRowOut(BaseModel):
    """Строка в выдаче."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    position: int
    status: BulkRowStatus
    error_message: str | None = None
    rate_quote_id: UUID | None = None
    recommendation_id: UUID | None = None
    decision_id: UUID | None = None
    shipment_id: UUID | None = None
    recipient_snapshot: dict[str, object]
    cargo_snapshot: dict[str, object]


class BulkRunOut(BaseModel):
    """Прогон целиком."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    status: BulkRunStatus
    strategy: RoutingStrategy | None = None
    sender_snapshot: dict[str, object]
    created_at: datetime
    updated_at: datetime
    rows: list[BulkRowOut] = Field(default_factory=list)

    #: Сводка по строкам: сколько в каком состоянии. Частичный успех —
    #: нормальное состояние прогона, и его надо видеть, не пересчитывая
    #: строки на фронте.
    counts: dict[str, int] = Field(default_factory=dict)


class BulkRunPage(BaseModel):
    """Страница списка прогонов."""

    items: list[BulkRunOut]
    total: int
