"""Эндпоинты отправлений."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from aerogram.core.deps import (
    CurrentPrincipal,
    SessionDep,
    SettingsDep,
    client_ip,
    require_roles,
)
from aerogram.core.service import AuditService
from aerogram.directories.deps import DadataDep
from aerogram.shared.enums import UserRole
from aerogram.shared.idempotency import IdempotencyKey
from aerogram.shipments.schemas import CreateShipmentRequest, ShipmentOut, ShipmentPage
from aerogram.shipments.service import ShipmentService

__all__ = ["shipments_router"]

shipments_router = APIRouter(prefix="/shipments", tags=["Отправления"])


@shipments_router.get("", response_model=ShipmentPage, summary="Список отправлений")
async def list_shipments(
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
    shipment_status: Annotated[str | None, Query(alias="status", max_length=30)] = None,
    carrier_id: Annotated[UUID | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ShipmentPage:
    """Отправления тенанта с фильтрами контракта.

    Поиск идёт по нашему номеру, треку перевозчика и его идентификатору
    заказа: оператор держит в руках что-то одно из трёх.
    """
    return await ShipmentService(session, settings, dadata).page(
        status=shipment_status, carrier_id=carrier_id, q=q, page=page, page_size=page_size
    )


@shipments_router.post(
    "",
    response_model=ShipmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать отправление по решению",
)
async def create_shipment(
    payload: CreateShipmentRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
    idempotency_key: IdempotencyKey,
    principal: Annotated[object, require_roles(UserRole.OWNER, UserRole.LOGISTICIAN)],
) -> ShipmentOut:
    """Создать заказ у перевозчика по принятому решению.

    Повтор с тем же ключом не создаёт второго заказа. Если предыдущая
    попытка не дошла до подтверждения, платформа сначала спрашивает
    перевозчика о заказе с нашим номером и только потом создаёт (FR-2.5).
    """
    actor: CurrentPrincipal = principal  # type: ignore[assignment]
    shipment = await ShipmentService(session, settings, dadata).create(
        payload,
        tenant_id=actor.tenant_id,
        user_id=actor.user_id,
        idempotency_key=idempotency_key,
    )
    AuditService(session).record(
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="shipment.create",
        entity_type="shipment",
        entity_id=shipment.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return shipment


@shipments_router.get("/{shipment_id}", response_model=ShipmentOut, summary="Отправление")
async def get_shipment(
    shipment_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
) -> ShipmentOut:
    return await ShipmentService(session, settings, dadata).get(shipment_id)


@shipments_router.post(
    "/{shipment_id}/cancel",
    response_model=ShipmentOut,
    summary="Отменить отправление",
)
async def cancel_shipment(
    shipment_id: UUID,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
    principal: Annotated[object, require_roles(UserRole.OWNER, UserRole.LOGISTICIAN)],
) -> ShipmentOut:
    """Отменить, пока перевозчик принимает отмену (FR-2.6).

    Отправление не удаляется никогда: отмена — это состояние и отметка
    времени, а не исчезновение строки.
    """
    actor: CurrentPrincipal = principal  # type: ignore[assignment]
    shipment = await ShipmentService(session, settings, dadata).cancel(
        shipment_id, tenant_id=actor.tenant_id
    )
    AuditService(session).record(
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="shipment.cancel",
        entity_type="shipment",
        entity_id=shipment.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return shipment
