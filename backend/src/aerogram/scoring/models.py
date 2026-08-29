"""Снапшоты Carrier Score.

Платформенная витрина: скор считается по всем тенантам сразу, поэтому tenant_id и RLS
здесь нет. Изменение весов не переписывает историю — версия формулы лежит в снапшоте
(FR-7.4).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
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
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from aerogram.db import Base, uuid_pk
from aerogram.shared.enums import ScoreConfidence, ScoreScope

__all__ = ["CarrierScoreSnapshot"]


class CarrierScoreSnapshot(Base):
    """Скор перевозчика в заданном разрезе за период."""

    __tablename__ = "carrier_score_snapshots"

    id: Mapped[UUID] = uuid_pk()
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[ScoreScope] = mapped_column(String(20), nullable=False)
    #: Ключ разреза: '' для global, 'RU-PRI>RU-MOW' для direction и т. п.
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)

    on_time_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    avg_delay_days: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    reliability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    incident_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    price_index: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    data_quality: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))

    score: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[ScoreConfidence] = mapped_column(String(20), nullable=False)
    formula_version: Mapped[str] = mapped_column(String(20), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "carrier_id",
            "scope_type",
            "scope_key",
            "period_start",
            "period_end",
            "formula_version",
            name="uq_carrier_score_snapshots_scope_period",
        ),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="score_range"),
        CheckConstraint("period_end >= period_start", name="score_period_order"),
        Index(
            "ix_carrier_score_snapshots_lookup",
            "carrier_id",
            "scope_type",
            "scope_key",
            "period_end",
        ),
    )
