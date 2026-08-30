"""DTO отправлений. Соответствуют схемам ``docs/tz/v3/openapi.yaml``."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from aerogram.shared.enums import ShipmentStatus
from aerogram.shared.schemas import MoneySchema

__all__ = [
    "CONTRACT_STATUS",
    "CreateShipmentRequest",
    "ShipmentOut",
    "ShipmentPage",
    "contract_status",
]

#: Наш словарь состояний богаче словаря контракта: ТЗ, раздел 9, требует
#: четырнадцати значений, схема ``Shipment`` перечисляет десять. Перевод
#: нужен на границе API, и он с потерями — несколько наших состояний
#: сходятся в одно значение контракта.
#:
#: Расхождение вынесено в docs/status.md: «возврат» и «неудачная попытка
#: вручения» превращаются в ``Exception``, хотя это разные события с разными
#: последствиями для клиента. Внутри они по-прежнему различимы, теряется
#: только внешнее представление.
CONTRACT_STATUS: dict[ShipmentStatus, str] = {
    ShipmentStatus.DRAFT: "Draft",
    ShipmentStatus.CREATED: "Created",
    ShipmentStatus.ACCEPTED: "Created",
    ShipmentStatus.PICKED_UP: "PickedUp",
    ShipmentStatus.AT_ORIGIN_HUB: "InTransit",
    ShipmentStatus.IN_TRANSIT: "InTransit",
    ShipmentStatus.AT_DESTINATION_HUB: "InTransit",
    ShipmentStatus.OUT_FOR_DELIVERY: "OutForDelivery",
    ShipmentStatus.READY_FOR_PICKUP: "OutForDelivery",
    ShipmentStatus.DELIVERY_ATTEMPT_FAILED: "Exception",
    ShipmentStatus.DELIVERED: "Delivered",
    ShipmentStatus.RETURN_IN_PROGRESS: "Exception",
    ShipmentStatus.RETURNED: "Exception",
    ShipmentStatus.CANCELLED: "Cancelled",
    ShipmentStatus.EXCEPTION: "Exception",
}


def contract_status(status: ShipmentStatus | str) -> str:
    """Наше состояние → значение контракта.

    Неизвестное значение отдаётся как ``Exception``, а не как есть: клиент
    сверяет ответ с перечислением схемы, и незнакомая строка сломала бы ему
    разбор. ``Exception`` при этом честен — состояние действительно
    непонятное.
    """
    try:
        return CONTRACT_STATUS[ShipmentStatus(status)]
    except ValueError:
        return "Exception"


class CreateShipmentRequest(BaseModel):
    """Создание отправления по принятому решению (схема ``CreateShipmentRequest``).

    ``external_id`` — номер клиента в его собственной системе. Если он задан,
    именно он становится номером отправления: интеграция с ERP хочет видеть
    свой номер и в кабинете, и у перевозчика. Не задан — номер выдаём мы.
    """

    decision_id: UUID
    external_id: str | None = Field(default=None, min_length=1, max_length=30)


class ShipmentOut(BaseModel):
    """Отправление (схема ``Shipment``).

    ``number`` в схеме контракта отсутствует, но FR-2.4 требует отдавать
    внутренний номер: по нему идёт разговор с перевозчиком и по нему же
    выполняется сверка «призраков».
    """

    id: UUID
    number: str
    #: Идентификатор заказа В СИСТЕМЕ ПЕРЕВОЗЧИКА (бэкенд-ТЗ, раздел 5).
    #: Пустой, пока перевозчик не подтвердил создание.
    external_id: str | None
    decision_id: UUID | None
    carrier_id: UUID
    carrier_name: str | None
    tracking_number: str | None
    status: str
    eta: datetime | None
    deadline: datetime | None
    quoted_total_cost: MoneySchema
    actual_total_cost: MoneySchema | None
    created_at: datetime


class ShipmentPage(BaseModel):
    """Страница списка отправлений. Параметры страницы — как в контракте."""

    items: list[ShipmentOut]
    total: int
    page: int
    page_size: int
