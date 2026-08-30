"""Эндпоинт расчёта."""

from __future__ import annotations

from fastapi import APIRouter

from aerogram.core.deps import CurrentPrincipal, SessionDep, SettingsDep
from aerogram.rating.schemas import RateRequestIn, RateResponse
from aerogram.rating.service import RateShoppingService

__all__ = ["rating_router"]

rating_router = APIRouter(prefix="/rates", tags=["Расчёт"])


@rating_router.post("", response_model=RateResponse, summary="Расчёт стоимости и срока")
async def calculate_rates(
    payload: RateRequestIn,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> RateResponse:
    """Опросить подключённых перевозчиков и вернуть выдачу (FR-1).

    Ответ успешен даже когда ни один перевозчик не ответил: неответившие
    перечислены в ``errors`` отдельными строками с причиной. Ошибка одного
    перевозчика не роняет выдачу (FR-1.4), а отсутствие выдачи целиком —
    это тоже результат расчёта, а не отказ сервиса.
    """
    return await RateShoppingService(session, settings).quote(
        payload, tenant_id=principal.tenant_id, user_id=principal.user_id
    )
