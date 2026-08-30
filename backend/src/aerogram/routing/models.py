"""Модели Decision Engine: рекомендация, решение, правила маршрутизации.

Три сущности, ради которых существует продукт. Рекомендация фиксирует, что
система предложила и на каком основании; решение — что человек или правило
выбрали; правила — корпоративную политику, ограничивающую выбор.

Снимок решения неизменяем (продуктовое ТЗ, раздел 8). Это гарантируется не
только соглашением: у ``decisions`` нет ``updated_at``, а триггер
``decisions_immutable`` в миграции 0007 отклоняет попытку сменить
рекомендацию, выбранный вариант, режим или момент решения.
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aerogram.db import Base, TenantMixin, uuid_pk
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import (
    DecisionMode,
    OverrideReason,
    RoutingStrategy,
    ScoreConfidence,
)

__all__ = ["Decision", "Recommendation", "RoutingRule"]


class Recommendation(Base, TenantMixin):
    """Что система рекомендовала по одному расчёту и одной стратегии.

    ``explanation`` хранит структурированные факты, а не готовую фразу:
    «уложился в срок», «дороже дешёвого на столько-то», «риск такой-то».
    Текст на русском собирает интерфейс — иначе объяснение нельзя ни
    перевести, ни пересобрать при смене формулировок (системное ТЗ, раздел 9).

    ``recommended_offer_id`` допускает NULL: если в срок не уложился никто,
    рекомендации нет, и подставлять вместо неё первый попавшийся вариант
    значило бы выдать нарушение дедлайна за совет.
    """

    __tablename__ = "recommendations"

    id: Mapped[UUID] = uuid_pk()
    quote_id: Mapped[UUID] = mapped_column(
        ForeignKey("rate_quotes.id", ondelete="CASCADE"), nullable=False
    )
    recommended_offer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("rate_offers.id", ondelete="RESTRICT")
    )
    strategy: Mapped[RoutingStrategy] = mapped_column(String(20), nullable=False)
    explanation: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    #: Насколько рекомендованный вариант отличается от альтернатив: разница
    #: в цене против самого дешёвого, в вероятности против самого надёжного.
    alternatives_delta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: Версии, без которых историческую рекомендацию нельзя воспроизвести.
    algorithm_version: Mapped[str] = mapped_column(String(40), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[ScoreConfidence | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    decisions: Mapped[list[Decision]] = relationship(back_populates="recommendation")

    __table_args__ = (
        Index("ix_recommendations_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_recommendations_quote_id", "quote_id"),
    )


class Decision(Base, TenantMixin):
    """Подтверждённый выбор: человеком или правилом автовыбора.

    ``override`` означает, что выбран не рекомендованный вариант. Причина
    обязательна и берётся из закрытого списка: свободный текст не сворачивается
    в метрику Override Rate, ради которой поле и существует. Развёрнутое
    пояснение живёт рядом, в ``override_comment``.
    """

    __tablename__ = "decisions"

    id: Mapped[UUID] = uuid_pk()
    recommendation_id: Mapped[UUID] = mapped_column(
        ForeignKey("recommendations.id", ondelete="RESTRICT"), nullable=False
    )
    selected_offer_id: Mapped[UUID] = mapped_column(
        ForeignKey("rate_offers.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    mode: Mapped[DecisionMode] = mapped_column(String(10), nullable=False)
    override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    override_reason: Mapped[OverrideReason | None] = mapped_column(String(30))
    override_comment: Mapped[str | None] = mapped_column(Text)
    #: Ключ идемпотентности запроса и отпечаток его тела: повтор с тем же ключом
    #: и тем же телом обязан вернуть тот же результат, с другим телом — 409.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
    )

    recommendation: Mapped[Recommendation] = relationship(back_populates="decisions")

    __table_args__ = (
        CheckConstraint(
            "NOT override OR override_reason IS NOT NULL", name="override_states_the_reason"
        ),
        CheckConstraint(
            "mode <> 'manual' OR actor_id IS NOT NULL", name="manual_decision_has_an_actor"
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_decisions_tenant_id_idempotency_key"
        ),
        Index("ix_decisions_tenant_id_decided_at", "tenant_id", "decided_at"),
        Index("ix_decisions_recommendation_id", "recommendation_id"),
    )


class RoutingRule(Base, TenantMixin):
    """Правило корпоративной политики: whitelist, запрет, порог страхования.

    ``conditions`` и ``actions`` — JSONB намеренно: их состав меняется вместе
    с политикой тенанта, и раскладывать его по колонкам значило бы миграцию
    на каждое новое условие. По ним не фильтруют в БД — правила читаются
    целиком и применяются в памяти.

    Приоритет уникален внутри тенанта: два правила с одинаковым приоритетом
    дали бы разный результат при разном порядке чтения строк.
    """

    __tablename__ = "routing_rules"

    id: Mapped[UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Версия политики попадает в снимок рекомендации: без неё нельзя понять,
    #: по каким правилам было принято историческое решение.
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
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
        CheckConstraint("priority >= 0", name="priority_non_negative"),
        UniqueConstraint("tenant_id", "priority", name="uq_routing_rules_tenant_id_priority"),
        Index("ix_routing_rules_tenant_id_enabled_priority", "tenant_id", "enabled", "priority"),
    )
