"""Перечисления домена, общие для всех модулей."""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "FINAL_STATUSES",
    "CargoType",
    "CarrierAccountMode",
    "CreatedVia",
    "DeliveryMode",
    "DocumentFormat",
    "DocumentType",
    "EventSource",
    "LabelFormat",
    "PriceSource",
    "ScoreConfidence",
    "ScoreScope",
    "SelectionRule",
    "ShipmentStatus",
    "TenantStatus",
    "UserRole",
]


class ShipmentStatus(StrEnum):
    """Нормализованная модель состояний (раздел 9 ТЗ).

    Каждый адаптер обязан отобразить свои статусы в этот набор.
    """

    CREATED = "CREATED"
    ACCEPTED = "ACCEPTED"
    PICKED_UP = "PICKED_UP"
    AT_ORIGIN_HUB = "AT_ORIGIN_HUB"
    IN_TRANSIT = "IN_TRANSIT"
    AT_DESTINATION_HUB = "AT_DESTINATION_HUB"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    DELIVERY_ATTEMPT_FAILED = "DELIVERY_ATTEMPT_FAILED"
    DELIVERED = "DELIVERED"
    RETURN_IN_PROGRESS = "RETURN_IN_PROGRESS"
    RETURNED = "RETURNED"
    CANCELLED = "CANCELLED"
    EXCEPTION = "EXCEPTION"


#: Финальные статусы: переход в любой из них останавливает polling (раздел 9 ТЗ).
FINAL_STATUSES: frozenset[ShipmentStatus] = frozenset(
    {
        ShipmentStatus.DELIVERED,
        ShipmentStatus.RETURNED,
        ShipmentStatus.CANCELLED,
    }
)


class TenantStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class UserRole(StrEnum):
    """Роли внутри тенанта и роли платформы (раздел 2 ТЗ)."""

    OWNER = "owner"
    LOGISTICIAN = "logistician"
    OPERATOR = "operator"
    VIEWER = "viewer"
    API_CLIENT = "api_client"
    PLATFORM_ADMIN = "platform_admin"
    SUPPORT = "support"


class CarrierAccountMode(StrEnum):
    OWN_CONTRACT = "own_contract"
    AEROGRAM = "aerogram"


class PriceSource(StrEnum):
    OWN_CONTRACT = "own_contract"
    AEROGRAM = "aerogram"
    PUBLIC = "public"


class CargoType(StrEnum):
    DOCUMENTS = "documents"
    PARCEL = "parcel"
    CARGO = "cargo"
    EQUIPMENT = "equipment"


class DeliveryMode(StrEnum):
    DOOR_DOOR = "door_door"
    DOOR_TERMINAL = "door_terminal"
    TERMINAL_DOOR = "terminal_door"
    TERMINAL_TERMINAL = "terminal_terminal"


class EventSource(StrEnum):
    API_POLL = "api_poll"
    WEBHOOK = "webhook"
    MANUAL = "manual"
    #: Заложено в MVP под Cargo Control очереди 2 (раздел 5.4 ТЗ): события датчиков
    #: пишутся в ту же ленту, что и статусы перевозчика, без миграции схемы.
    SENSOR = "sensor"


class DocumentType(StrEnum):
    LABEL = "label"
    WAYBILL = "waybill"
    MANIFEST = "manifest"
    INVENTORY = "inventory"
    ACCEPTANCE_REGISTER = "acceptance_register"


class DocumentFormat(StrEnum):
    PDF = "pdf"
    ZPL = "zpl"
    PNG = "png"


class LabelFormat(StrEnum):
    PDF_A4 = "pdf_a4"
    PDF_A5 = "pdf_a5"
    PDF_A6 = "pdf_a6"
    ZPL = "zpl"


class CreatedVia(StrEnum):
    WEB = "web"
    API = "api"
    IMPORT = "import"


class ScoreScope(StrEnum):
    GLOBAL = "global"
    DIRECTION = "direction"
    DIRECTION_WEIGHT = "direction_weight"


class ScoreConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT = "insufficient"


class SelectionRule(StrEnum):
    """Правила автовыбора для пакетных сценариев и API (FR-5.5)."""

    CHEAPEST = "cheapest"
    FASTEST = "fastest"
    BEST_SCORE = "best_score"
    BEST_VALUE = "best_value"
    CHEAPEST_MEETING_DEADLINE = "cheapest_meeting_deadline"
