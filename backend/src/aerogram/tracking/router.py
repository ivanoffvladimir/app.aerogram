"""Эндпоинты трекинга."""

from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from aerogram.core.deps import CurrentPrincipal, SessionDep, SettingsDep, require_roles
from aerogram.shared.enums import UserRole
from aerogram.shared.errors import NotFound, ValidationFailed
from aerogram.shipments.repository import ShipmentRepository
from aerogram.tracking.inbound import InboundWebhookService
from aerogram.tracking.schemas import (
    TrackingEventOut,
    WebhookSubscriptionCreated,
    WebhookSubscriptionIn,
    WebhookSubscriptionOut,
)
from aerogram.tracking.service import TrackingService
from aerogram.tracking.webhooks import WebhookService

__all__ = ["tracking_router", "webhooks_router"]

tracking_router = APIRouter(prefix="/shipments", tags=["Трекинг"])


@tracking_router.get(
    "/{shipment_id}/tracking",
    response_model=list[TrackingEventOut],
    summary="Лента событий отправления",
)
async def timeline(
    shipment_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> list[TrackingEventOut]:
    """Единая лента независимо от перевозчика (FR-3.4).

    Существование отправления проверяется отдельно: пустая лента у чужого
    отправления и пустая лента у своего выглядели бы одинаково, а это разные
    ответы — 404 и 200.
    """
    if await ShipmentRepository(session).get(shipment_id) is None:
        raise NotFound("Отправление не найдено")
    return await TrackingService(session).timeline(shipment_id)


webhooks_router = APIRouter(prefix="/webhooks", tags=["Вебхуки"])


@webhooks_router.get(
    "/subscriptions",
    response_model=list[WebhookSubscriptionOut],
    summary="Подписки на события",
)
async def list_subscriptions(
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> list[WebhookSubscriptionOut]:
    rows = await WebhookService(session, settings).subscriptions()
    return [WebhookSubscriptionOut.model_validate(row, from_attributes=True) for row in rows]


@webhooks_router.post(
    "/subscriptions",
    response_model=WebhookSubscriptionCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Подписаться на события",
)
async def subscribe(
    payload: WebhookSubscriptionIn,
    session: SessionDep,
    settings: SettingsDep,
    principal: Annotated[object, require_roles(UserRole.OWNER, UserRole.LOGISTICIAN)],
) -> WebhookSubscriptionCreated:
    """Подписаться на события отправлений (FR-3.6).

    Секрет подписи возвращается **один раз**: у нас он хранится зашифрованным
    и восстановлению не подлежит. Он нужен получателю, чтобы проверять подпись
    `X-Aerogram-Signature` — HMAC-SHA256 над временем и телом.
    """
    actor: CurrentPrincipal = principal  # type: ignore[assignment]
    subscription, secret = await WebhookService(session, settings).subscribe(
        tenant_id=actor.tenant_id, url=payload.url, events=payload.events
    )
    return WebhookSubscriptionCreated(
        **WebhookSubscriptionOut.model_validate(subscription, from_attributes=True).model_dump(),
        secret=secret,
    )


@webhooks_router.delete(
    "/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отключить подписку",
)
async def unsubscribe(
    subscription_id: UUID,
    session: SessionDep,
    settings: SettingsDep,
    principal: Annotated[object, require_roles(UserRole.OWNER, UserRole.LOGISTICIAN)],
) -> None:
    """Отключить подписку. Строка остаётся — по ней читается история доставок."""
    await WebhookService(session, settings).unsubscribe(subscription_id)


@webhooks_router.post(
    "/{carrier_code}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Приём вебхука перевозчика",
)
async def carrier_webhook(
    carrier_code: str,
    request: Request,
    settings: SettingsDep,
) -> Response:
    """Событие от перевозчика в ленту отправления (FR-3.1).

    Путь **без авторизации** — так он и объявлен в контракте: перевозчик
    не носит наш токен. Вместо токена подпись, и проверяется она секретом
    того тенанта, чьё отправление названо в теле (ADR-0015).

    Ответ 202 и на неизвестный заказ: перевозчик повторял бы доставку
    бесконечно из-за заказа, созданного не через нас.
    """
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        raise ValidationFailed("Тело вебхука не является JSON") from None
    if not isinstance(payload, dict):
        raise ValidationFailed("Тело вебхука должно быть объектом")

    service = InboundWebhookService(settings)
    accepted = await service.accept(
        carrier_code,
        payload,
        body=body,
        # Заголовки приводятся к нижнему регистру: у HTTP регистр не значим,
        # а адаптер иначе искал бы X-Signature и не находил x-signature.
        headers={k.lower(): v for k, v in request.headers.items()},
    )
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"accepted": accepted})
