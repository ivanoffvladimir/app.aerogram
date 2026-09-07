"""Эндпоинты печатных форм.

Файл отдаётся с нашего сервера под обычной сессией, а не подписанной
ссылкой: в этикетке персональные данные получателя, и ссылка-предъявитель
на них жила бы сутки у всякого, кому её переслали (ADR-0016).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from aerogram.core.deps import CurrentPrincipal, SessionDep, SettingsDep
from aerogram.documents.schemas import DocumentOut, LabelRequestIn
from aerogram.documents.service import DocumentService

__all__ = ["documents_router"]

documents_router = APIRouter(tags=["Документы"])


@documents_router.get(
    "/shipments/{shipment_id}/documents",
    response_model=list[DocumentOut],
    summary="Документы отправления",
)
async def list_documents(
    shipment_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> list[DocumentOut]:
    """Что уже заказано по отправлению, включая неготовое и неудавшееся."""
    return await DocumentService(session, settings).of_shipment(shipment_id)


@documents_router.post(
    "/shipments/{shipment_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Заказать этикетку у перевозчика",
)
async def order_label(
    shipment_id: UUID,
    payload: LabelRequestIn,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> DocumentOut:
    """Заказать печатную форму.

    Повтор не стоит второго обращения к перевозчику: форма той же тройки
    «отправление, тип, формат» возвращается как есть. У Почты России вызов
    тратит суточную квоту, величина которой нам неизвестна, — платить за
    каждое нажатие кнопки незачем.

    ``201`` и на уже существующем документе: тело одно и то же, а различать
    «создано сейчас» и «создано минуту назад» клиенту не по чему и незачем.
    """
    return await DocumentService(session, settings).label(shipment_id, payload)


@documents_router.get(
    "/documents/{document_id}/content",
    summary="Файл документа",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "Файл документа"}},
)
async def document_content(
    document_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Отдать файл.

    Целиком, а не потоком: этикетка — десятки килобайт, и потоковая отдача
    усложнила бы код ради экономии, которой нет.
    """
    body, content_type, filename = await DocumentService(session, settings).content(document_id)
    return Response(
        content=body,
        media_type=content_type,
        headers={
            # ``attachment`` намеренно: этикетку печатают, а не читают
            # на экране, и открытая в браузере вкладка с персональными
            # данными получателя переживает саму задачу.
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Персональные данные не должны оседать в общих кэшах.
            "Cache-Control": "private, no-store",
        },
    )
