"""DTO трекинга. Соответствуют схеме ``TrackingEvent`` из openapi.yaml."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "ShipmentExceptionOut",
    "ShipmentExceptionsPage",
    "TrackingEventOut",
    "WebhookSubscriptionCreated",
    "WebhookSubscriptionIn",
    "WebhookSubscriptionOut",
]


class TrackingEventOut(BaseModel):
    """Событие ленты в едином виде, независимо от перевозчика (FR-3.4).

    ``carrier_status`` отдаётся рядом с нормализованным намеренно: когда
    оператор звонит перевозчику, разговаривать он будет на языке перевозчика,
    а не на нашем.
    """

    occurred_at: datetime
    normalized_status: str
    carrier_status: str
    location: str | None = None
    description: str | None = None


class ShipmentExceptionOut(BaseModel):
    """Строка разбора исключений.

    Причин может быть несколько сразу: сорванный срок и молчание перевозчика
    — разные беды, и схлопывать их в одну значило бы потерять половину.
    """

    id: UUID
    number: str
    carrier_name: str | None
    tracking_number: str | None
    status: str
    deadline: datetime | None
    last_event_at: datetime | None
    reasons: list[str]


class ShipmentExceptionsPage(BaseModel):
    """Разбор исключений целиком.

    ``scanned`` и ``truncated`` показаны намеренно: список ограничен сверху,
    и оператор обязан видеть, что дальше предела осталось непросмотренное,
    а не считать пустоту за порядок.
    """

    items: list[ShipmentExceptionOut]
    total: int
    scanned: int
    truncated: bool
    by_reason: dict[str, int]


class WebhookSubscriptionIn(BaseModel):
    """Подписка на события отправлений (FR-3.6)."""

    url: str = Field(min_length=1, max_length=1000)
    events: list[str] = Field(min_length=1)


class WebhookSubscriptionOut(BaseModel):
    """Подписка. Секрет здесь отсутствует и появиться не может."""

    id: UUID
    url: str
    events: list[str]
    is_active: bool
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int


class WebhookSubscriptionCreated(WebhookSubscriptionOut):
    """Ответ на создание подписки.

    ``secret`` показывается **один раз**: восстановить его нельзя, у нас он
    лежит зашифрованным. Тем же правилом живут API-ключи (FR-10.2).
    """

    secret: str
