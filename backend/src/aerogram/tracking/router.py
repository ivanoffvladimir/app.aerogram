"""Эндпоинты трекинга."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, status

from aerogram.core.deps import CurrentPrincipal, SessionDep, SettingsDep, require_roles
from aerogram.shared.enums import UserRole
from aerogram.shared.errors import NotFound
from aerogram.shipments.repository import ShipmentRepository
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
