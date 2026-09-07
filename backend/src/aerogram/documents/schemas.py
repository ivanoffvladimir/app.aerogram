"""DTO документов.

Схем документа в замороженном контракте нет: там `Documents: label/waybill
where available` только во фронт-ТЗ. Поэтому состав задан по модели.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aerogram.shared.enums import DocumentFormat, DocumentType, LabelFormat

__all__ = ["DocumentOut", "LabelRequestIn"]


class LabelRequestIn(BaseModel):
    """Заказ печатной формы у перевозчика.

    Тип не спрашивается: формировать мы умеем ровно этикетку. Накладную
    контракт адаптера отдельным вызовом не отдаёт, а обещать её полем
    запроса значило бы обещать несделанное.
    """

    format: LabelFormat = LabelFormat.PDF_A4


class DocumentOut(BaseModel):
    """Документ в кабинете.

    Ссылки на файл здесь нет намеренно: файл отдаётся отдельным путём под
    обычной сессией. Подписанная ссылка была бы предъявительским токеном
    на персональные данные получателя, живущим у всякого, кому её переслали
    (ADR-0016).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    shipment_id: UUID | None
    type: DocumentType
    format: DocumentFormat
    #: `pending` — перевозчик формирует форму; `ready` — файл лежит у нас;
    #: `failed` — не получилось, и причина названа рядом.
    status: str
    size_bytes: int | None
    error: str | None
    generated_at: datetime | None
    created_at: datetime
