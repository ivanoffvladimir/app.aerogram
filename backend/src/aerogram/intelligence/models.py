"""Снапшоты Carrier Score и платформенная база под ними.

**Снапшот принадлежит тенанту** (ADR-0017): отсюда ``tenant_id``, RLS
и ``tenant_id`` первым в ключе уникальности — без него пересчёт одного тенанта
затирал бы снапшот другого, а витрина отдавала бы чужие числа.

**Платформенная база — единственная таблица модуля без тенанта**, и это
не забытая политика, а её суть (ADR-0026): свод по перевозчику существует
затем, чтобы новый клиент увидел оценку до своего первого отправления.
Обезличенность обеспечивается не показом, а самой таблицей: ограничения
запрещают строку, посчитанную меньше чем по трём клиентам. Пороги записаны
числами в ``CHECK``, а не константами Python, намеренно — их изменение
обязано проходить через миграцию и ревью, потому что это решение о чужих
данных.

Изменение весов не переписывает историю — версия формулы лежит и в снапшоте,
и в базе (FR-7.4).
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

from aerogram.db import Base, TenantMixin, uuid_pk
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import ScoreBasis, ScoreConfidence, ScoreScope

__all__ = ["CarrierPlatformBaseline", "CarrierScoreSnapshot"]


class CarrierScoreSnapshot(Base, TenantMixin):
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
    #: На чьих данных стоит число: платформенных, своих или обоих. Хранится
    #: рядом со скором, а не выводится при показе: снапшот неизменяем, и
    #: основание — часть ответа, а не его оформление.
    basis: Mapped[ScoreBasis] = mapped_column(
        String(20), nullable=False, default=ScoreBasis.NONE, server_default=ScoreBasis.NONE.value
    )
    #: Размер платформенной выборки, на которую опирался этот снапшот.
    #: ``NULL`` — базы не было. Нужен, чтобы объяснить оператору число,
    #: посчитанное не по его отправлениям.
    platform_sample_size: Mapped[int | None] = mapped_column(Integer)
    formula_version: Mapped[str] = mapped_column(String(20), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
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


class CarrierPlatformBaseline(Base):
    """Свод по перевозчику через всех клиентов платформы.

    Тенанта у таблицы нет и быть не должно: строка описывает перевозчика,
    а не клиента, и показывается всем сразу. Взамен изоляции её защищают
    два ограничения и одно отсутствие:

    * ``tenants_count >= 3`` — свод, посчитанный по одному или двум клиентам,
      это их статистика, выданная остальным. При двух каждый вычитает себя
      и получает числа второго почти точно;
    * ``sample_size >= 30`` — число, которое видит каждый клиент, не должно
      стоять на десяти отправлениях;
    * **колонки цены здесь нет вовсе.** Тариф — коммерческая тайна клиента,
      и усреднённый по платформе он рассказал бы соседям об уровне чужих
      договоров. Индекс цены считается только внутри тенанта.
    """

    __tablename__ = "carrier_platform_baselines"

    id: Mapped[UUID] = uuid_pk()
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    #: Сколько РАЗНЫХ клиентов дали хотя бы одно завершённое отправление.
    tenants_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)

    on_time_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    reliability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    incident_free: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    data_quality: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))

    formula_version: Mapped[str] = mapped_column(String(20), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "carrier_id",
            "period_start",
            "period_end",
            "formula_version",
            name="uq_carrier_platform_baselines_period",
        ),
        # Пороги числами, а не константами Python: их изменение — решение
        # о чужих данных, и оно обязано проходить миграцией через ревью.
        CheckConstraint("tenants_count >= 3", name="platform_baseline_anonymity"),
        CheckConstraint("sample_size >= 30", name="platform_baseline_sample"),
        CheckConstraint("period_end >= period_start", name="platform_baseline_period_order"),
        Index(
            "ix_carrier_platform_baselines_lookup",
            "carrier_id",
            "period_end",
        ),
    )
