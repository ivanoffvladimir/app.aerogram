"""Перечисления домена, общие для всех модулей."""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "FINAL_STATUSES",
    "PLATFORM_ROLES",
    "PROBLEM_STATUSES",
    "STALLED_INCIDENT",
    "CargoType",
    "CarrierAccountMode",
    "CostComponentType",
    "CreatedVia",
    "DecisionMode",
    "DeliveryMode",
    "DocumentFormat",
    "DocumentType",
    "EventSource",
    "IneligibilityReason",
    "LabelFormat",
    "OfferSource",
    "OverrideReason",
    "PriceSource",
    "ProbabilityLabel",
    "RiskLevel",
    "RoutingStrategy",
    "ScoreConfidence",
    "ScoreScope",
    "SelectionRule",
    "ShipmentStatus",
    "TenantRole",
    "TenantStatus",
    "UserRole",
]


class ShipmentStatus(StrEnum):
    """Нормализованная модель состояний (раздел 9 ТЗ).

    Каждый адаптер обязан отобразить свои статусы в этот набор.
    """

    #: Наше собственное состояние, а не статус перевозчика: намерение создать
    #: заказ записано, но подтверждения от ТК ещё нет. Существует ради того,
    #: чтобы потерянный ответ не оставил заказ-«призрак» без записи (FR-2.5):
    #: номер уже выдан и по нему заказ можно найти у перевозчика.
    #: Ни один адаптер в него не отображает свои статусы.
    DRAFT = "DRAFT"
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


#: Состояния, которые сами по себе означают разбор: неудачное вручение,
#: возврат, исключение перевозчика. Живут здесь, а не в трекинге, потому что
#: набор нужен и уведомлениям тенанту, и экрану разбора, и сводке кабинета —
#: а сводке запрещено видеть перевозчиков даже транзитом (CLAUDE.md §4).
PROBLEM_STATUSES: frozenset[ShipmentStatus] = frozenset(
    {
        ShipmentStatus.EXCEPTION,
        ShipmentStatus.DELIVERY_ATTEMPT_FAILED,
        ShipmentStatus.RETURN_IN_PROGRESS,
        ShipmentStatus.RETURNED,
    }
)

#: Тип инцидента «перевозчик молчит дольше порога опроса». Строка, а не
#: перечисление: колонка ``shipments.incident_type`` текстовая, и типов
#: со временем станет больше.
STALLED_INCIDENT = "stalled"

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
    """Все роли системы: и внутри тенанта, и платформенные.

    Это словарь ХРАНЕНИЯ и проверки прав. Привязывать его к телу запроса
    нельзя: тогда владелец тенанта смог бы выдать себе платформенную роль
    и получить доступ к общим справочникам всех тенантов. Для входных данных
    существует ``TenantRole`` — в нём платформенных ролей нет физически.
    """

    OWNER = "owner"
    LOGISTICIAN = "logistician"
    OPERATOR = "operator"
    VIEWER = "viewer"
    API_CLIENT = "api_client"
    PLATFORM_ADMIN = "platform_admin"
    SUPPORT = "support"


class TenantRole(StrEnum):
    """Роли, которые владелец тенанта вправе выдать внутри своего тенанта.

    Отдельный тип, а не подмножество с проверкой: значения, которого нет
    в перечислении, Pydantic не примет вовсе, и забыть проверку негде.
    Платформенные роли выдаются только вне продуктового API.
    """

    OWNER = "owner"
    LOGISTICIAN = "logistician"
    OPERATOR = "operator"
    VIEWER = "viewer"
    API_CLIENT = "api_client"


#: Роли платформы. Стоят НАД тенантом: доступ к общим справочникам,
#: которые читаются на горячем пути расчёта у всех тенантов сразу.
PLATFORM_ROLES: frozenset[UserRole] = frozenset({UserRole.PLATFORM_ADMIN, UserRole.SUPPORT})


class CarrierAccountMode(StrEnum):
    OWN_CONTRACT = "own_contract"
    AEROGRAM = "aerogram"


class PriceSource(StrEnum):
    OWN_CONTRACT = "own_contract"
    AEROGRAM = "aerogram"
    PUBLIC = "public"


class RoutingStrategy(StrEnum):
    """Стратегия выбора. Значения — из схемы контракта."""

    OPTIMAL = "optimal"
    CHEAPEST = "cheapest"
    FASTEST = "fastest"
    RELIABLE = "reliable"


class OfferSource(StrEnum):
    """Чей тариф: договор клиента или тариф платформы.

    Ровно два значения контракта (``RateOffer.source`` в openapi.yaml).
    Публичный расчёт ПЭК в эту пару не укладывается — см. «Требует решения
    человека» в docs/status.md.
    """

    CLIENT_CONTRACT = "client_contract"
    LOGISTICS_OS = "logistics_os"


class CostComponentType(StrEnum):
    """Составляющая стоимости. Значения — из схемы ``CostComponent``."""

    BASE = "base"
    INSURANCE = "insurance"
    PICKUP = "pickup"
    DOOR_DELIVERY = "door_delivery"
    PACKAGING = "packaging"
    REMOTE_AREA = "remote_area"
    PALLET = "pallet"
    WAITING = "waiting"
    DECLARED_VALUE = "declared_value"
    OTHER = "other"


class DecisionMode(StrEnum):
    """Кто принял решение: человек или правило автовыбора."""

    MANUAL = "manual"
    AUTO = "auto"


class OverrideReason(StrEnum):
    """Почему выбран не рекомендованный вариант (фронт-ТЗ, раздел 5).

    Список закрытый: свободный текст не сворачивается в метрику Override Rate,
    ради которой это поле и существует. Развёрнутое пояснение живёт рядом,
    в ``override_comment``.
    """

    CHEAPER = "cheaper"
    FASTER = "faster"
    RECIPIENT_REQUIREMENT = "recipient_requirement"
    CORPORATE_POLICY = "corporate_policy"
    NEGATIVE_EXPERIENCE = "negative_experience"
    CARRIER_PREFERENCE = "carrier_preference"
    OTHER = "other"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ProbabilityLabel(StrEnum):
    """Категория вероятности доставки в срок. Показывается вместо голого процента."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IneligibilityReason(StrEnum):
    """Почему предложение не участвует в рекомендации.

    Не укладывающиеся в дедлайн строки не скрываются, а показываются ниже
    с указанием причины (продуктовое ТЗ, раздел 7).
    """

    MISSES_DEADLINE = "misses_deadline"
    CARRIER_BLACKLISTED = "carrier_blacklisted"
    NOT_IN_WHITELIST = "not_in_whitelist"
    SERVICE_UNAVAILABLE = "service_unavailable"
    CARGO_RESTRICTED = "cargo_restricted"
    TENANT_POLICY = "tenant_policy"


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
