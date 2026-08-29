"""Модели трекинга.

Порядок событий восстанавливается по ``occurred_at``, а не по времени получения:
перевозчики регулярно отдают события с задержкой и не по порядку (раздел 9 ТЗ).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
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
from aerogram.shared.enums import EventSource, ShipmentStatus

__all__ = ["ShipmentEvent", "WebhookDelivery", "WebhookSubscription"]


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
        DateTime(timezone=True), nullable=False, server_default=text("now()")
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
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_webhook_deliveries_next_attempt_at", "next_attempt_at"),
        Index("ix_webhook_deliveries_subscription_id_created_at", "subscription_id", "created_at"),
    )
