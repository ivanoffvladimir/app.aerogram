"""Эндпоинты Carrier Intelligence."""

from __future__ import annotations

from fastapi import APIRouter

from aerogram.core.deps import CurrentPrincipal, SessionDep
from aerogram.intelligence.schemas import CarrierAnalyticsOut
from aerogram.intelligence.service import ScoreService

__all__ = ["analytics_router"]

analytics_router = APIRouter(prefix="/analytics", tags=["Аналитика"])


@analytics_router.get(
    "/carriers",
    response_model=list[CarrierAnalyticsOut],
    summary="Carrier Score по перевозчикам",
)
async def carrier_analytics(
    principal: CurrentPrincipal,
    session: SessionDep,
) -> list[CarrierAnalyticsOut]:
    """Скор, доверие и расшифровка по компонентам.

    Перевозчик без накопленной статистики возвращается со `score = null`
    и `confidence = insufficient`: интерфейс обязан показать «недостаточно
    данных», а не число и не пустое место (FR-7.3, FR-7.5).
    """
    return await ScoreService(session).analytics()
