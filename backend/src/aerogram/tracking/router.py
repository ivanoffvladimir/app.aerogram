"""Эндпоинты трекинга."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from aerogram.core.deps import CurrentPrincipal, SessionDep
from aerogram.shared.errors import NotFound
from aerogram.shipments.repository import ShipmentRepository
from aerogram.tracking.schemas import TrackingEventOut
from aerogram.tracking.service import TrackingService

__all__ = ["tracking_router"]

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
