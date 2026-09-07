"""Репозиторий документов. Единственное место с SQL в модуле."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.documents.models import Document
from aerogram.shared.enums import FINAL_STATUSES, DocumentFormat, DocumentType
from aerogram.shipments.models import Shipment

__all__ = ["DocumentRepository"]


class DocumentRepository:
    """Печатные формы тенанта.

    Тенант нигде не указывается в условии: его ставит RLS, и дублировать
    её здесь значило бы завести второе место, где можно ошибиться.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, document: Document) -> Document:
        self._session.add(document)
        return document

    async def get(self, document_id: UUID) -> Document | None:
        return await self._session.get(Document, document_id)

    async def of_shipment(self, shipment_id: UUID) -> list[Document]:
        """Документы отправления, свежие сверху."""
        stmt = (
            select(Document)
            .where(Document.shipment_id == shipment_id)
            .order_by(Document.created_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars())

    async def find(
        self, shipment_id: UUID, doc_type: DocumentType, fmt: DocumentFormat
    ) -> Document | None:
        """Уже заказанный документ той же тройки.

        Нужен затем, чтобы повторное нажатие «Скачать» не стоило второго
        обращения к перевозчику: у Почты России оно тратит суточную квоту,
        величина которой нам неизвестна.
        """
        stmt = select(Document).where(
            Document.shipment_id == shipment_id,
            Document.type == doc_type,
            Document.format == fmt,
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def expired_files(
        self, finished_before: datetime, *, limit: int
    ) -> list[tuple[Document, str]]:
        """Готовые файлы отправлений, пришедших к финалу раньше срока.

        «Пришло к финалу» — это ``cancelled_at`` у отменённого и последнее
        событие у доставленного: после финального статуса опрос
        останавливается, и другого события уже не будет. ``created_at``
        подстрахует случай, когда ни того ни другого нет, — иначе запрос
        молча пропустил бы такую строку и файл жил бы вечно.

        Возвращается пара с ключом: он ``NULL``-абельный в схеме, и проверку
        на месте вызова легко забыть.
        """
        finished = func.coalesce(Shipment.cancelled_at, Shipment.last_event_at, Shipment.created_at)
        stmt = (
            select(Document, Document.s3_key)
            .join(Shipment, Shipment.id == Document.shipment_id)
            .where(
                Document.status == "ready",
                Document.s3_key.is_not(None),
                Shipment.status.in_(FINAL_STATUSES),
                finished < finished_before,
            )
            .order_by(finished)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(document, key) for document, key in rows if key]

    async def pending(self, *, older_than: datetime, limit: int) -> list[Document]:
        """Документы, которые перевозчик обещал сформировать.

        ``older_than`` отсекает только что заказанные: спрашивать о форме
        через секунду после заказа бессмысленно и тратит вызов.
        """
        stmt = (
            select(Document)
            .where(Document.status == "pending", Document.created_at <= older_than)
            .order_by(Document.created_at)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())
