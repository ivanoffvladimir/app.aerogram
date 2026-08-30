"""Модели расчёта.

Имена совпадают с контрактом (``docs/tz/v3/openapi.yaml``): ``RateQuote`` —
снимок запроса, ``RateOffer`` — одно предложение одного перевозчика.

Каждый запрос и каждое предложение сохраняются, включая сырой ответ ТК: это
исходные данные для Carrier Score и для разбора спорных ситуаций.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aerogram.db import Base, TenantMixin, uuid_pk
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import (
    CostComponentType,
    IneligibilityReason,
    OfferSource,
    PriceSource,
    ProbabilityLabel,
    RiskLevel,
)

__all__ = ["CostComponent", "RateOffer", "RateQuote"]


class RateQuote(Base, TenantMixin):
    """Снимок запроса расчёта со всеми полученными предложениями.

    ``input_snapshot`` неизменяем: на нём строится вся последующая аналитика,
    и пересчитывать его задним числом запрещено (продуктовое ТЗ, раздел 8).
    ``hash`` — ключ кэша нормализованного запроса.
    """

    __tablename__ = "rate_quotes"

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID | None] = mapped_column()
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy: Mapped[str | None] = mapped_column(String(20))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: true — в срок не укладывается ни одно предложение (контракт, RateResponse).
    no_deadline_match: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    offers: Mapped[list[RateOffer]] = relationship(
        back_populates="quote", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_rate_quotes_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_rate_quotes_tenant_id_hash", "tenant_id", "hash"),
        Index("ix_rate_quotes_valid_until", "valid_until"),
    )


class RateOffer(Base, TenantMixin):
    """Одно предложение одного перевозчика по одному тарифу.

    Строка с ``error_code`` — это перевозчик, не ответивший в срок или вернувший
    ошибку: он показывается в выдаче отдельной строкой, а не пропадает
    (системное ТЗ, раздел 8: partial success — нормальное состояние).

    Снимок неизменяем после принятия решения: рекомендация и решение ссылаются
    именно на эти цифры, и пересчёт задним числом обесценил бы всю аналитику.
    """

    __tablename__ = "rate_offers"

    id: Mapped[UUID] = uuid_pk()
    quote_id: Mapped[UUID] = mapped_column(
        ForeignKey("rate_quotes.id", ondelete="CASCADE"), nullable=False
    )
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="RESTRICT"), nullable=False
    )
    carrier_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("carrier_accounts.id", ondelete="SET NULL")
    )
    service_code: Mapped[str | None] = mapped_column(String(50))
    tariff_code: Mapped[str | None] = mapped_column(String(50))
    #: Total Cost в минорных единицах валюты ``currency`` (ADR-0011):
    #: базовый тариф, страхование и все обязательные доплаты.
    total_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="RUB")
    #: Чей тариф — договор клиента или тариф платформы (контракт, RateOffer.source).
    source: Mapped[OfferSource | None] = mapped_column(String(20))
    price_source: Mapped[PriceSource | None] = mapped_column(String(20))
    transit_days_min: Mapped[int | None] = mapped_column(Integer)
    transit_days_max: Mapped[int | None] = mapped_column(Integer)
    promised_delivery_date: Mapped[date | None] = mapped_column(Date)
    #: Момент доставки, а не дата: запас до дедлайна считается в секундах,
    #: а дедлайн задаётся датой ИЛИ датой со временем (продуктовое ТЗ, раздел 7).
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Участвует ли предложение в рекомендации. Непригодные не скрываются:
    #: они показываются ниже с причиной.
    eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    ineligibility_reason: Mapped[IneligibilityReason | None] = mapped_column(String(40))
    deadline_margin_seconds: Mapped[int | None] = mapped_column(BigInteger)
    lateness_seconds: Mapped[int | None] = mapped_column(BigInteger)
    #: Доля от 0 до 1, калиброванная по фактам, а не заявленный SLA перевозчика.
    on_time_probability: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    probability_label: Mapped[ProbabilityLabel | None] = mapped_column(String(10))
    risk: Mapped[RiskLevel | None] = mapped_column(String(10))
    #: Скор на момент выдачи — чтобы ретроспективно оценить качество рекомендаций (FR-7.6).
    score_at_quote: Mapped[int | None] = mapped_column(Integer)
    score_confidence: Mapped[str | None] = mapped_column(String(20))
    score_scope: Mapped[str | None] = mapped_column(String(20))
    rank: Mapped[int | None] = mapped_column(Integer)
    meets_deadline: Mapped[bool | None] = mapped_column()
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    quote: Mapped[RateQuote] = relationship(back_populates="offers")
    cost_components: Mapped[list[CostComponent]] = relationship(
        back_populates="offer", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint(
            "(total_amount_minor IS NOT NULL AND error_code IS NULL)"
            " OR (total_amount_minor IS NULL AND error_code IS NOT NULL)",
            name="total_xor_error",
        ),
        CheckConstraint(
            "total_amount_minor IS NULL OR total_amount_minor >= 0",
            name="total_non_negative",
        ),
        CheckConstraint(
            "on_time_probability IS NULL"
            " OR (on_time_probability >= 0 AND on_time_probability <= 1)",
            name="on_time_probability_is_a_probability",
        ),
        CheckConstraint(
            "eligible OR ineligibility_reason IS NOT NULL",
            name="ineligible_offer_states_the_reason",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_is_iso_4217"),
        Index("ix_rate_offers_quote_id", "quote_id"),
        Index("ix_rate_offers_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_rate_offers_quote_id_eligible", "quote_id", "eligible"),
    )


class CostComponent(Base, TenantMixin):
    """Составляющая Total Cost: база, страхование, надбавка.

    Хранится строками, а не одним JSON: расшифровка стоимости показывается
    оператору и участвует в сверке со счётом, то есть по ней фильтруют
    и суммируют. JSONB здесь означал бы разбор в приложении при каждом отчёте.
    """

    __tablename__ = "cost_components"

    id: Mapped[UUID] = uuid_pk()
    offer_id: Mapped[UUID] = mapped_column(
        ForeignKey("rate_offers.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[CostComponentType] = mapped_column(String(20), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    #: Ставка в процентах: 0.18 означает 0.18 %. Не деньги, поэтому Numeric.
    rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    offer: Mapped[RateOffer] = relationship(back_populates="cost_components")

    __table_args__ = (
        CheckConstraint("amount_minor >= 0", name="component_amount_non_negative"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_is_iso_4217"),
        CheckConstraint(
            "rate_percent IS NULL OR rate_percent >= 0", name="component_rate_non_negative"
        ),
        Index("ix_cost_components_offer_id", "offer_id"),
    )
