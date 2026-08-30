"""Модели трекинга.

Порядок событий восстанавливается по ``occurred_at``, а не по времени получения:
перевозчики регулярно отдают события с задержкой и не по порядку (раздел 9 ТЗ).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aerogram.db import Base, TenantMixin, TimestampMixin, uuid_pk
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import EventSource, ShipmentStatus

__all__ = ["DeliveryOutcome", "ShipmentEvent", "WebhookDelivery", "WebhookSubscription"]


class ShipmentEvent(Base, TenantMixin):
    """Событие ленты трекинга.

    ``source = sensor`` заложен уже в MVP под Cargo Control очереди 2 (раздел 5.4 ТЗ):
    события датчиков пишутся в ту же ленту без миграции схемы.
    """

    __tablename__ = "shipment_events"

    id: Mapped[UUID] = uuid_pk()
    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )
    status_normalized: Mapped[ShipmentStatus] = mapped_column(String(30), nullable=False)
    status_raw: Mapped[str] = mapped_column(String(255), nullable=False)
    #: true — сырой статус не удалось сопоставить, отправление попало в очередь
    #: ручного сопоставления в админке (раздел 9 ТЗ).
    is_unmapped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    city: Mapped[str | None] = mapped_column(String(255))
    comment: Mapped[str | None] = mapped_column(Text)
    source: Mapped[EventSource] = mapped_column(String(20), nullable=False)
    #: Отпечаток события у перевозчика — защита от дублей при polling + вебхуках.
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint(
            "shipment_id", "dedup_key", name="uq_shipment_events_shipment_id_dedup_key"
        ),
        CheckConstraint(
            "source IN ('api_poll', 'webhook', 'manual', 'sensor')", name="event_source"
        ),
        Index("ix_shipment_events_shipment_id_occurred_at", "shipment_id", "occurred_at"),
        Index("ix_shipment_events_tenant_id_received_at", "tenant_id", "received_at"),
    )


class WebhookSubscription(Base, TenantMixin, TimestampMixin):
    """Исходящие вебхуки тенанта (FR-3.6). Подпись — HMAC-SHA256."""

    __tablename__ = "webhook_subscriptions"

    id: Mapped[UUID] = uuid_pk()
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    events: Mapped[list[str]] = mapped_column(ARRAY(String(50)), nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_webhook_subscriptions_tenant_id_is_active", "tenant_id", "is_active"),
    )


class WebhookDelivery(Base, TenantMixin):
    """Попытка доставки исходящего вебхука: 5 попыток, экспоненциальная задержка."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[UUID] = uuid_pk()
    subscription_id: Mapped[UUID] = mapped_column(
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    shipment_id: Mapped[UUID | None] = mapped_column()
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    __table_args__ = (
        Index("ix_webhook_deliveries_next_attempt_at", "next_attempt_at"),
        Index("ix_webhook_deliveries_subscription_id_created_at", "subscription_id", "created_at"),
    )


class DeliveryOutcome(Base, TenantMixin):
    """Факт доставки: то, ради чего собирается вся история решений.

    Одна строка на отправление, поэтому первичный ключ — сам ``shipment_id``:
    отдельный суррогатный ключ допускал бы два противоречащих факта об одной
    доставке.

    Фактическая стоимость отделена от факта доставки намеренно: она приходит
    из счёта и может появиться сильно позже (системное ТЗ, раздел 10). Пока
    её нет, ``actual_amount_minor`` пуст, но ``delivered_at`` уже заполнен —
    и SLA считается, не дожидаясь бухгалтерии.
    """

    __tablename__ = "delivery_outcomes"

    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), primary_key=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Уложились ли в дедлайн. NULL означает «дедлайна не было», а не «неизвестно».
    deadline_met: Mapped[bool | None] = mapped_column(Boolean)
    delay_seconds: Mapped[int | None] = mapped_column(BigInteger)
    actual_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    damage: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    claim: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    claim_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "actual_amount_minor IS NULL OR actual_amount_minor >= 0",
            name="actual_cost_non_negative",
        ),
        CheckConstraint(
            "actual_amount_minor IS NULL OR currency IS NOT NULL",
            name="actual_cost_has_a_currency",
        ),
        CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="currency_is_iso_4217"),
        Index("ix_delivery_outcomes_tenant_id_delivered_at", "tenant_id", "delivered_at"),
    )
