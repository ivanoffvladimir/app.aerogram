"""Эндпоинты Decision Engine."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, status

from aerogram.core.deps import CurrentPrincipal, Principal, SessionDep, require_roles
from aerogram.routing.schemas import (
    DecisionOut,
    DecisionRequestIn,
    DecisionResponse,
    RecommendationOut,
    RoutingRequestIn,
    RoutingRuleIn,
    RoutingRuleOut,
    RoutingRulePatch,
    RoutingRulesOut,
)
from aerogram.routing.service import DecisionService, RecommendationService, RoutingRuleService
from aerogram.shared.enums import UserRole
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


@routing_router.get(
    "/decisions/{decision_id}",
    response_model=DecisionOut,
    summary="Снимок принятого решения",
)
async def read_decision(
    decision_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> DecisionOut:
    """Прочитать решение.

    Нужно карточке отправления и разбору спора: чем именно объясняется
    выбор — рекомендацией, причиной отказа от неё или правилом автовыбора.
    Снимок автовыбора виден иначе только в базе.
    """
    return await DecisionService(session).get(decision_id)


#: Правила — это корпоративная политика: они решают, кого можно спрашивать
#: и что обязательно страховать. Читать их вправе логист, менять — только
#: владелец: правка правил меняет исход каждого следующего расчёта, а отменить
#: её задним числом нельзя — решения уже приняты по прежней политике.
_READERS = require_roles(UserRole.OWNER, UserRole.LOGISTICIAN)
_WRITERS = require_roles(UserRole.OWNER)


@routing_router.get(
    "/routing-rules",
    response_model=RoutingRulesOut,
    summary="Правила маршрутизации",
)
async def list_rules(
    principal: Annotated[object, _READERS],
    session: SessionDep,
) -> RoutingRulesOut:
    """Правила тенанта, включая выключенные, и версия политики."""
    return await RoutingRuleService(session).list()


@routing_router.post(
    "/routing-rules",
    response_model=RoutingRuleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать правило маршрутизации",
)
async def create_rule(
    payload: RoutingRuleIn,
    principal: Annotated[Principal, _WRITERS],
    session: SessionDep,
) -> RoutingRuleOut:
    """Завести правило.

    Неизвестный ключ условия или действия отвергается здесь, а не при
    расчёте: опечатка в имени условия иначе превратила бы правило
    в «совпадает со всем» (ADR-0028).
    """
    return await RoutingRuleService(session).create(payload, tenant_id=principal.tenant_id)


@routing_router.patch(
    "/routing-rules/{rule_id}",
    response_model=RoutingRuleOut,
    summary="Изменить правило маршрутизации",
)
async def update_rule(
    rule_id: UUID,
    payload: RoutingRulePatch,
    principal: Annotated[object, _WRITERS],
    session: SessionDep,
) -> RoutingRuleOut:
    """Изменить правило. Не переданное поле не меняется."""
    return await RoutingRuleService(session).update(rule_id, payload)


@routing_router.delete(
    "/routing-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить правило маршрутизации",
)
async def delete_rule(
    rule_id: UUID,
    principal: Annotated[object, _WRITERS],
    session: SessionDep,
) -> None:
    """Удалить правило.

    Уже принятые решения не меняются: они несут версию прежней политики,
    и пересчёт истории по новым правилам сделал бы аналитику несопоставимой.
    """
    await RoutingRuleService(session).delete(rule_id)
