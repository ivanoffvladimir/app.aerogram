"""Модели массовых отправлений (ADR-0022).

Две таблицы, и ни одна не хранит того, что уже хранят ``rate_quotes``,
``recommendations``, ``decisions`` и ``shipments``, — только ссылается на них.

Отдельного признака «тариф заменён вручную» здесь намеренно нет: замена — это
``Decision`` с ``override = true`` и причиной из закрытого списка. Он уже
реализован, уже попадает в метрику Override Rate и уже виден на дашборде.
Продублировать смысл в двух местах значит однажды их рассинхронизировать.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
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
from aerogram.shared.enums import BulkRowStatus, BulkRunStatus, RoutingStrategy

__all__ = ["BulkRow", "BulkRun"]


class BulkRun(Base, TenantMixin):
    """Массовый расчёт: один отправитель, много получателей.

    ``sender_snapshot`` неизменяем: на нём строится вся выдача прогона,
    и правка карточки контрагента задним числом не должна её менять.
    """

    __tablename__ = "bulk_runs"

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    #: Имя задаётся шаблонно по дате и правится вручную.
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[BulkRunStatus] = mapped_column(
        String(20), nullable=False, default=BulkRunStatus.DRAFT, server_default=text("'draft'")
    )
    #: Приоритет выдачи — та же стратегия, что у одиночного расчёта.
    strategy: Mapped[RoutingStrategy | None] = mapped_column(String(20))
    sender_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
    )

    rows: Mapped[list[BulkRow]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="BulkRow.position",
    )

    __table_args__ = (
        Index("ix_bulk_runs_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_bulk_runs_tenant_id_status", "tenant_id", "status"),
    )


class BulkRow(Base, TenantMixin):
    """Одна строка списка: один получатель.

    Строка ссылается на расчёт, рекомендацию, решение и отправление — то есть
    на существующие сущности Decision Engine. Своих цифр она не хранит,
    поэтому пересчёт задним числом невозможен by construction.
    """

    __tablename__ = "bulk_rows"

    id: Mapped[UUID] = uuid_pk()
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("bulk_runs.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    cargo_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rate_quote_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("rate_quotes.id", ondelete="SET NULL")
    )
    recommendation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("recommendations.id", ondelete="SET NULL")
    )
    decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("decisions.id", ondelete="SET NULL")
    )
    shipment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("shipments.id", ondelete="SET NULL")
    )
    status: Mapped[BulkRowStatus] = mapped_column(
        String(20), nullable=False, default=BulkRowStatus.NEW, server_default=text("'new'")
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    run: Mapped[BulkRun] = relationship(back_populates="rows")

    __table_args__ = (
        UniqueConstraint("run_id", "position", name="uq_bulk_rows_run_id_position"),
        CheckConstraint(
            "status <> 'failed' OR error_message IS NOT NULL",
            name="failed_row_states_the_reason",
        ),
        CheckConstraint(
            "status <> 'created' OR shipment_id IS NOT NULL",
            name="created_row_has_a_shipment",
        ),
        Index("ix_bulk_rows_tenant_id_run_id", "tenant_id", "run_id"),
        Index("ix_bulk_rows_run_id_status", "run_id", "status"),
    )
