"""Модели документов.

Все сгенерированные документы лежат в S3 и доступны повторно без обращения к ТК
(FR-4.4). Ссылка наружу — подписанная, живёт 24 часа.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from aerogram.db import Base, TenantMixin, uuid_pk
from aerogram.shared.enums import DocumentFormat, DocumentType

__all__ = ["Document"]


class Document(Base, TenantMixin):
    """Печатная форма.

    ``status = pending`` — форма заказана у перевозчика и ещё готовится: асинхронность
    печатной формы скрыта от клиента адаптером (FR-4.5), но состояние видно в UI.
    """

    __tablename__ = "documents"

    id: Mapped[UUID] = uuid_pk()
    shipment_id: Mapped[UUID | None] = mapped_column(ForeignKey("shipments.id", ondelete="CASCADE"))
    #: Для сводных форм (реестр приёма-передачи) — список отправлений.
    shipment_ids: Mapped[list[UUID] | None] = mapped_column(ARRAY(String(36)))
    type: Mapped[DocumentType] = mapped_column(String(30), nullable=False)
    format: Mapped[DocumentFormat] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    s3_key: Mapped[str | None] = mapped_column(String(500))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'ready', 'failed')", name="document_status"),
        Index("ix_documents_shipment_id_type", "shipment_id", "type"),
        Index("ix_documents_tenant_id_created_at", "tenant_id", "created_at"),
    )
