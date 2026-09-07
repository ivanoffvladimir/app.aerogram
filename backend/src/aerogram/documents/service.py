"""Печатные формы: заказ у перевозчика, хранение, отдача (ADR-0016).

ТЗ v3 требует от этого модуля одного: `Documents: label/waybill where
available` в карточке отправления. Ни пакетной печати, ни собственных форм —
реестра, описи, манифеста — в v3 нет; они из архива v1. Поэтому здесь нет
и библиотеки PDF: этикетка приезжает от перевозчика готовыми байтами,
мы её кладём и отдаём.

**Зачем хранить, а не спрашивать каждый раз.** Обращение за формой стоит
вызова к перевозчику, а у Почты России — ещё и суточной квоты, величина
которой нам неизвестна. Оператор жмёт «Скачать» столько раз, сколько нужно
складу; платить за это перевозчику незачем.

**Файл отдаётся через наш API, а не подписанной ссылкой.** В этикетке ФИО,
адрес и телефон получателя. Подписанная ссылка — предъявительский токен
на эти данные: её пересылают, она остаётся в истории браузера и в логах
прокси, и отозвать её нельзя. Требования подписанной ссылки в v3 нет.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.config import Settings
from aerogram.documents.merge import merge_pdfs
from aerogram.documents.models import Document
from aerogram.documents.repository import DocumentRepository
from aerogram.documents.schemas import BatchLabelsOut, DocumentOut, LabelRequestIn
from aerogram.documents.storage import CONTENT_TYPES, ObjectStorage, document_key
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import DocumentFormat, DocumentType, LabelFormat
from aerogram.shared.errors import AerogramError, Conflict, NotFound
from aerogram.shared.ids import uuid7
from aerogram.shared.logging import get_logger
from aerogram.shipments.models import Shipment
from aerogram.shipments.service import ShipmentService

__all__ = ["GIVE_UP_AFTER", "PENDING_BATCH", "PENDING_DELAY", "DocumentService"]

log = get_logger(__name__)

#: Формат этикетки → формат хранимого документа. Перевозчик отдаёт лист A4,
#: A5 или A6 — для нас это один и тот же PDF, размер листа живёт в самом
#: файле. ZPL хранится как есть: это язык принтера, а не документ.
_STORED_FORMAT: dict[LabelFormat, DocumentFormat] = {
    LabelFormat.PDF_A4: DocumentFormat.PDF,
    LabelFormat.PDF_A5: DocumentFormat.PDF,
    LabelFormat.PDF_A6: DocumentFormat.PDF,
    LabelFormat.ZPL: DocumentFormat.ZPL,
}

#: Через сколько после заказа имеет смысл спросить форму снова. Спрашивать
#: через секунду — тратить вызов на заведомое «ещё не готово».
PENDING_DELAY = timedelta(minutes=2)

#: Когда перестать спрашивать. Счётчика попыток в схеме нет, и заводить его
#: значило бы менять таблицу — построчное ревью человека (CLAUDE.md §7).
#: Возраст отвечает на тот же вопрос и честнее: сутки без формы означают,
#: что её не будет, сколько ни спрашивай.
GIVE_UP_AFTER = timedelta(hours=24)

#: Сколько документов дотягивается за один проход по тенанту. Недотянутые
#: вернутся следующим циклом — они никуда не денутся.
PENDING_BATCH = 50

#: Срок хранения документов и снимков отправлений, лет (ADR-0030). Решение
#: человека: транспортные и курьерские накладные — первичные документы,
#: и срок у них не наш.
#:
#: Число живёт здесь, а не в трёх местах: политика в кабинете, опись
#: и сторожевой тест берут его отсюда. Разойдись копии — каждая соврала бы
#: своё, и заметили бы это через годы.
RETENTION_YEARS = 5


class DocumentService:
    """Документы отправлений тенанта."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._documents = DocumentRepository(session)
        self._shipments = ShipmentService(session, settings)
        self._storage = ObjectStorage(settings)

    # --- Чтение -----------------------------------------------------------

    async def of_shipment(self, shipment_id: UUID) -> list[DocumentOut]:
        """Документы отправления.

        Отправление проверяется отдельно: без этого пустой список у чужого
        отправления был бы неотличим от пустого у своего, то есть отвечал бы
        на вопрос «а есть ли такое отправление» (CLAUDE.md §6).
        """
        await self._shipments.require(shipment_id)
        found = await self._documents.of_shipment(shipment_id)
        return [DocumentOut.model_validate(document) for document in found]

    async def content(self, document_id: UUID) -> tuple[bytes, str, str]:
        """Байты документа, тип содержимого и имя файла.

        Ключ в хранилище берётся из строки, которую тенанту отдала база:
        RLS — единственная и достаточная проверка прав. В самом хранилище
        разграничения нет и быть не может.
        """
        document = await self._require(document_id)
        if document.status != "ready" or not document.s3_key:
            raise Conflict("Документ ещё не готов", field="document_id")
        fmt = DocumentFormat(document.format)
        body = await self._storage.get(document.s3_key)
        return body, CONTENT_TYPES[fmt], f"{document.type}-{document.id}.{fmt.value}"

    # --- Заказ ------------------------------------------------------------

    async def label(self, shipment_id: UUID, payload: LabelRequestIn) -> DocumentOut:
        """Заказать этикетку у перевозчика или вернуть уже заказанную.

        Повтор не стоит второго обращения к перевозчику: документ той же
        тройки «отправление, тип, формат» отдаётся как есть — включая
        неготовый и включая неудавшийся. Последнее намеренно: молча
        повторять заказ после отказа значило бы бить в перевозчика на каждое
        нажатие кнопки. Повторную попытку делает подметание, а окончательный
        отказ виден в кабинете с причиной.
        """
        shipment = await self._shipments.require(shipment_id)
        stored = _STORED_FORMAT[payload.format]

        existing = await self._documents.find(shipment_id, DocumentType.LABEL, stored)
        if existing is not None:
            return DocumentOut.model_validate(existing)

        document = Document(
            id=uuid7(),
            tenant_id=shipment.tenant_id,
            shipment_id=shipment_id,
            type=DocumentType.LABEL,
            format=stored,
            status="pending",
        )
        self._documents.add(document)
        await self._session.flush()

        await self._pull(document, payload.format)
        return DocumentOut.model_validate(document)

    # --- Пакетная печать ---------------------------------------------------

    async def order_labels(self, shipment_ids: Sequence[UUID]) -> BatchLabelsOut:
        """Заказать этикетки по списку отправлений.

        Уже заказанные не заказываются заново: у Почты России вызов тратит
        суточную квоту, и повторная кнопка «Печать» не должна её жечь.
        """
        ready = pending = failed = 0
        for shipment_id in shipment_ids:
            document = await self.label(shipment_id, LabelRequestIn())
            if document.status == "ready":
                ready += 1
            elif document.status == "pending":
                pending += 1
            else:
                failed += 1
        log.info("documents.batch_ordered", ready=ready, pending=pending, failed=failed)
        return BatchLabelsOut(ready=ready, pending=pending, failed=failed)

    async def merged_labels(self, shipment_ids: Sequence[UUID]) -> tuple[bytes, int]:
        """Одна пачка PDF по списку отправлений. Возвращает файл и число страниц.

        **Склеенный файл не хранится.** Он производный: каждая этикетка уже
        лежит у нас по отдельности, и вторая копия означала бы вторую копию
        персональных данных получателей в хранилище — ради файла, который
        собирается за миллисекунды. Заодно снимается вопрос устаревания:
        добавили строку в прогон, нажали печать — пачка уже с ней.

        Порядок сохраняется тот, в котором пришли отправления: на складе
        пачка раскладывается вместе со списком прогона.
        """
        parts: list[bytes] = []
        for shipment_id in shipment_ids:
            document = await self._documents.find(
                shipment_id, DocumentType.LABEL, DocumentFormat.PDF
            )
            if document is None or document.status != "ready" or not document.s3_key:
                continue
            parts.append(await self._storage.get(document.s3_key))

        if not parts:
            raise Conflict("Ни одной готовой этикетки нет", field="run_id")

        result = merge_pdfs(parts)
        if result.merged == 0:
            # Все файлы оказались нечитаемыми: пустой PDF на принтере хуже
            # честного отказа — кладовщик решит, что печатать нечего.
            raise Conflict("Ни одна этикетка не читается", field="run_id")
        log.info(
            "documents.batch_merged",
            merged=result.merged,
            skipped=result.skipped,
            pages=result.page_count,
        )
        return result.content, result.page_count

    async def fetch_pending(self, *, now: datetime | None = None) -> int:
        """Дотянуть формы, которые перевозчик обещал сформировать (FR-4.5).

        Подметание по расписанию, а не отложенная задача на документ:
        состояние живёт в таблице, поэтому перезапуск брокера не оставляет
        документ висеть в ``pending`` навсегда.
        """
        moment = now or utcnow()
        pulled = 0
        for document in await self._documents.pending(
            older_than=moment - PENDING_DELAY, limit=PENDING_BATCH
        ):
            if moment - document.created_at > GIVE_UP_AFTER:
                document.status = "failed"
                document.error = "Перевозчик не сформировал документ за сутки"
                log.info("documents.given_up", document_id=str(document.id))
                continue
            # Формат заказа восстанавливается по хранимому: лист A4 — наш
            # выбор по умолчанию, а разницу между A4, A5 и A6 таблица
            # не хранит, потому что для нас это один и тот же PDF.
            zpl = document.format == DocumentFormat.ZPL
            requested = LabelFormat.ZPL if zpl else LabelFormat.PDF_A4
            if await self._pull(document, requested):
                pulled += 1
        return pulled

    # --- Вспомогательное ---------------------------------------------------

    async def _pull(self, document: Document, fmt: LabelFormat) -> bool:
        """Спросить форму у перевозчика и сохранить, если она готова.

        Возвращает, появился ли файл. Отказ перевозчика гасит документ,
        а не запрос: оператор просил этикетку, и `500` вместо строки
        «не получилось, вот почему» не сказал бы ему ничего.
        """
        if document.shipment_id is None:
            return False
        shipment: Shipment = await self._shipments.require(document.shipment_id)
        if not shipment.external_id:
            document.status = "failed"
            document.error = "У отправления нет номера у перевозчика"
            return False

        try:
            adapter, account = await self._shipments.adapter_for(shipment)
            result = await adapter.label(shipment.external_id, fmt, account)
        except Exception as exc:
            expected = isinstance(exc, AerogramError)
            document.status = "failed"
            # Текст наших доменных ошибок писан нами и персональных данных
            # не содержит; у чужого исключения берётся только имя класса —
            # в его сообщении может оказаться адрес из тела запроса.
            document.error = str(exc) if expected else type(exc).__name__
            if expected:
                log.info("documents.failed", document_id=str(document.id), error=document.error)
            else:
                log.exception("documents.crashed", document_id=str(document.id))
            return False

        if result.is_pending or result.content is None:
            # Штатное состояние, а не ошибка: подметание вернётся за файлом.
            return False

        stored = DocumentFormat(document.format)
        key = document_key(document.tenant_id, shipment.id, document.id, stored)
        await self._storage.put(key, result.content, content_type=CONTENT_TYPES[stored])
        document.s3_key = key
        document.size_bytes = len(result.content)
        document.status = "ready"
        document.error = None
        document.generated_at = utcnow()
        log.info("documents.stored", document_id=str(document.id), size=document.size_bytes)
        return True

    async def _require(self, document_id: UUID) -> Document:
        document = await self._documents.get(document_id)
        if document is None:
            # Чужой документ RLS не отдаёт вовсе, и это тот же 404.
            raise NotFound("Документ не найден")
        return document
