"""Модели отправлений.

Отправления не удаляются никогда (раздел 7.1 ТЗ): отмена — это статус ``CANCELLED``
и ``cancelled_at``, а не удаление строки.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aerogram.db import Base, TenantMixin, TimestampMixin, uuid_pk
from aerogram.shared.enums import CreatedVia, ShipmentStatus

__all__ = ["Shipment", "ShipmentItem", "ShipmentPlace"]

#: Условие «отправление ещё в работе». Вынесено в константу, потому что тот же
#: предикат используется частичными индексами и планировщиком polling.
_ACTIVE = text("status NOT IN ('DELIVERED', 'RETURNED', 'CANCELLED')")


class Shipment(Base, TenantMixin, TimestampMixin):
    """Отправление — сущность Aerogram, соответствующая заказу у перевозчика."""

    __tablename__ = "shipments"

    id: Mapped[UUID] = uuid_pk()
    #: Внутренний человекочитаемый номер, показывается в кабинете и в реестрах.
    number: Mapped[str] = mapped_column(String(30), nullable=False)
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="RESTRICT"), nullable=False
    )
    carrier_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("carrier_accounts.id", ondelete="SET NULL")
    )
    #: Идентификатор заказа в системе перевозчика.
    external_id: Mapped[str | None] = mapped_column(String(100))
    tracking_number: Mapped[str | None] = mapped_column(String(100))
    service_code: Mapped[str | None] = mapped_column(String(50))
    tariff_code: Mapped[str | None] = mapped_column(String(50))

    status: Mapped[ShipmentStatus] = mapped_column(
        String(30), nullable=False, default=ShipmentStatus.CREATED
    )
    carrier_status_raw: Mapped[str | None] = mapped_column(String(255))

    sender_address_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("addresses.id", ondelete="RESTRICT")
    )
    recipient_address_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("addresses.id", ondelete="RESTRICT")
    )
    #: Решение, из которого создано отправление (контракт, CreateShipmentRequest).
    #: NULL допустим для отправлений, созданных до появления Decision Engine
    #: и для прямого создания в обход расчёта.
    decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("decisions.id", ondelete="RESTRICT")
    )
    #: Снимок адресов на момент создания: адресная книга может измениться,
    #: а отправление обязано остаться воспроизводимым в спорной ситуации.
    sender_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    recipient_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    #: Все суммы отправления — в минорных единицах валюты ``currency`` (ADR-0011).
    #: Валюта одна на отправление: перевозчик выставляет счёт в одной валюте,
    #: и вторая колонка у строк означала бы возможность расхождения с шапкой.
    declared_value_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="RUB")
    payment_type: Mapped[str] = mapped_column(String(20), nullable=False, default="sender")
    price_quoted_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    price_actual_amount_minor: Mapped[int | None] = mapped_column(BigInteger)

    promised_delivery_date: Mapped[date | None] = mapped_column(Date)
    actual_delivery_date: Mapped[date | None] = mapped_column(Date)
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transit_days_planned: Mapped[int | None] = mapped_column(Integer)
    transit_days_actual: Mapped[int | None] = mapped_column(Integer)
    is_late: Mapped[bool | None] = mapped_column(Boolean)
    delay_days: Mapped[int | None] = mapped_column(Integer)

    has_incident: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    incident_type: Mapped[str | None] = mapped_column(String(50))

    created_via: Mapped[CreatedVia] = mapped_column(
        String(20), nullable=False, default=CreatedVia.WEB
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column()
    #: Выбранное предложение. Отдельно от ``decision_id``: решение может быть
    #: удалено политикой хранения, а номер предложения нужен для сверки со счётом.
    rate_offer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("rate_offers.id", ondelete="SET NULL")
    )
    #: Ключ идемпотентности первого успешного создания (FR-2.3), для сверки «призраков».
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    #: Последнее событие трекинга — держится денормализованно ради списка отправлений.
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)

    places: Mapped[list[ShipmentPlace]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan", lazy="selectin"
    )
    items: Mapped[list[ShipmentItem]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_shipments_tenant_id_number"),
        # Один заказ у перевозчика = одно отправление. Защита от дублей на уровне БД,
        # а не только на уровне ключа идемпотентности (FR-2.3, FR-2.5).
        UniqueConstraint("carrier_id", "external_id", name="uq_shipments_carrier_id_external_id"),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_shipments_tenant_id_idempotency_key"
        ),
        CheckConstraint(
            "price_quoted_amount_minor IS NULL OR price_quoted_amount_minor >= 0",
            name="shipment_price_quoted_non_negative",
        ),
        CheckConstraint(
            "price_actual_amount_minor IS NULL OR price_actual_amount_minor >= 0",
            name="shipment_price_actual_non_negative",
        ),
        CheckConstraint(
            "declared_value_amount_minor IS NULL OR declared_value_amount_minor >= 0",
            name="shipment_declared_value_non_negative",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_is_iso_4217"),
        Index("ix_shipments_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_shipments_decision_id", "decision_id"),
        Index("ix_shipments_tracking_number", "tracking_number"),
        # Частичный индекс по незавершённым: планировщик polling обращается к нему
        # каждую минуту, без него он сканирует всю таблицу (раздел 7.3 ТЗ).
        Index(
            "ix_shipments_active_next_poll",
            "next_poll_at",
            postgresql_where=_ACTIVE,
        ),
        Index(
            "ix_shipments_tenant_id_status_active",
            "tenant_id",
            "status",
            postgresql_where=_ACTIVE,
        ),
    )


class ShipmentPlace(Base, TenantMixin):
    """Место (грузовое) отправления."""

    __tablename__ = "shipment_places"

    id: Mapped[UUID] = uuid_pk()
    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    length_cm: Mapped[int] = mapped_column(Integer, nullable=False)
    width_cm: Mapped[int] = mapped_column(Integer, nullable=False)
    height_cm: Mapped[int] = mapped_column(Integer, nullable=False)
    volume_weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    barcode: Mapped[str | None] = mapped_column(String(100))

    shipment: Mapped[Shipment] = relationship(back_populates="places")

    __table_args__ = (
        UniqueConstraint("shipment_id", "number", name="uq_shipment_places_shipment_id_number"),
        CheckConstraint("weight_kg > 0", name="place_weight_positive"),
        CheckConstraint(
            "length_cm > 0 AND width_cm > 0 AND height_cm > 0", name="place_dimensions_positive"
        ),
    )


class ShipmentItem(Base, TenantMixin):
    """Товарное наполнение: опись вложения и объявленная ценность (FR-4.3)."""

    __tablename__ = "shipment_items"

    id: Mapped[UUID] = uuid_pk()
    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: В минорных единицах валюты отправления (``Shipment.currency``).
    price_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    nds_rate: Mapped[int | None] = mapped_column(Integer)
    marking_code: Mapped[str | None] = mapped_column(String(255))

    shipment: Mapped[Shipment] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="item_quantity_positive"),
        CheckConstraint("price_amount_minor >= 0", name="item_price_non_negative"),
        Index("ix_shipment_items_shipment_id", "shipment_id"),
    )
