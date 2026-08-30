"""Эндпоинты Decision Engine."""

from __future__ import annotations

from fastapi import APIRouter, status

from aerogram.core.deps import CurrentPrincipal, SessionDep
from aerogram.routing.schemas import (
    DecisionRequestIn,
    DecisionResponse,
    RecommendationOut,
    RoutingRequestIn,
)
from aerogram.routing.service import DecisionService, RecommendationService
from aerogram.shared.idempotency import IdempotencyKey

__all__ = ["routing_router"]

routing_router = APIRouter(tags=["Решения"])


@routing_router.post(
    "/routing/quote",
    response_model=RecommendationOut,
    summary="Рекомендация по расчёту",
)
async def recommend(
    payload: RoutingRequestIn,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> RecommendationOut:
    """Построить рекомендацию по уже полученному расчёту.

    Рекомендация сохраняется вместе с версиями формулы и политики: без них
    историческое решение нельзя ни воспроизвести, ни сравнить с нынешним.
    """
    return await RecommendationService(session).recommend(payload, tenant_id=principal.tenant_id)


@routing_router.post(
    "/decisions",
    response_model=DecisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Подтверждение выбора",
)
async def decide(
    payload: DecisionRequestIn,
    principal: CurrentPrincipal,
    session: SessionDep,
    idempotency_key: IdempotencyKey,
) -> DecisionResponse:
    """Зафиксировать решение неизменяемым снимком.

    Повтор с тем же ключом и тем же телом возвращает то же решение и не
    создаёт второго. Тот же ключ с другим телом даёт ``409``: клиент,
    изменивший запрос, ждёт нового действия, и молча отдать ему прошлый
    результат хуже, чем отказать.
    """
    return await DecisionService(session).decide(
        payload,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        idempotency_key=idempotency_key,
    )
