"""Модели расчёта.

Каждый запрос и каждая котировка сохраняются, включая сырой ответ ТК (FR-1.7):
это исходные данные для Carrier Score и для разбора спорных ситуаций.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aerogram.db import Base, TenantMixin, uuid_pk
from aerogram.shared.enums import PriceSource

__all__ = ["RateQuote", "RateRequest"]


class RateRequest(Base, TenantMixin):
    """Один запрос расчёта. ``hash`` — ключ кэша нормализованного запроса (FR-1.6)."""

    __tablename__ = "rate_requests"

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID | None] = mapped_column()
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    quotes: Mapped[list[RateQuote]] = relationship(
        back_populates="request", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_rate_requests_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_rate_requests_tenant_id_hash", "tenant_id", "hash"),
    )


class RateQuote(Base, TenantMixin):
    """Одно предложение одного ТК по одному тарифу.

    Строка с ``error_code`` — это перевозчик, не ответивший в срок или вернувший
    ошибку (FR-1.4): он показывается в выдаче отдельной строкой, а не пропадает.
    """

    __tablename__ = "rate_quotes"

    id: Mapped[UUID] = uuid_pk()
    rate_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("rate_requests.id", ondelete="CASCADE"), nullable=False
    )
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="RESTRICT"), nullable=False
    )
    carrier_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("carrier_accounts.id", ondelete="SET NULL")
    )
    service_code: Mapped[str | None] = mapped_column(String(50))
    tariff_code: Mapped[str | None] = mapped_column(String(50))
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    price_source: Mapped[PriceSource | None] = mapped_column(String(20))
    transit_days_min: Mapped[int | None] = mapped_column(Integer)
    transit_days_max: Mapped[int | None] = mapped_column(Integer)
    promised_delivery_date: Mapped[date | None] = mapped_column(Date)
    #: Скор на момент выдачи — чтобы ретроспективно оценить качество рекомендаций (FR-7.6).
    score_at_quote: Mapped[int | None] = mapped_column(Integer)
    score_confidence: Mapped[str | None] = mapped_column(String(20))
    score_scope: Mapped[str | None] = mapped_column(String(20))
    rank: Mapped[int | None] = mapped_column(Integer)
    meets_deadline: Mapped[bool | None] = mapped_column()
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )

    request: Mapped[RateRequest] = relationship(back_populates="quotes")

    __table_args__ = (
        CheckConstraint(
            "(price IS NOT NULL AND error_code IS NULL)"
            " OR (price IS NULL AND error_code IS NOT NULL)",
            name="quote_price_xor_error",
        ),
        CheckConstraint("price IS NULL OR price >= 0", name="quote_price_non_negative"),
        Index("ix_rate_quotes_rate_request_id", "rate_request_id"),
        Index("ix_rate_quotes_tenant_id_created_at", "tenant_id", "created_at"),
    )
