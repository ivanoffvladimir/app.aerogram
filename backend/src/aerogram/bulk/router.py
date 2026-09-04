"""Эндпоинты массовых отправлений (ADR-0022).

Прогон движется тремя явными шагами — посчитать, выбрать, оформить, — а не
одной кнопкой. Так оператор видит выдачу до того, как что-то оформлено,
и может заменить тариф по любой строке обычным ``POST /v1/decisions``
с ``override``: отдельного пути для замены нет намеренно.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from aerogram.bulk.repository import BulkRepository
from aerogram.bulk.schemas import (
    BulkImportIn,
    BulkImportOut,
    BulkRunCreateIn,
    BulkRunOut,
    BulkRunPage,
    BulkRunRenameIn,
)
from aerogram.bulk.service import BulkService
from aerogram.core.deps import CurrentPrincipal, SessionDep, SettingsDep, require_roles
from aerogram.directories.deps import DadataDep
from aerogram.rating.service import RateShoppingService
from aerogram.routing.service import DecisionService, RecommendationService
from aerogram.shared.enums import UserRole
from aerogram.shipments.service import ShipmentService

__all__ = ["bulk_router"]

bulk_router = APIRouter(prefix="/bulk-runs", tags=["Массовые отправления"])

#: Оформление списком — действие с деньгами, поэтому оно за теми же ролями,
#: что и одиночное оформление.
_CAN_SHIP = require_roles(UserRole.OWNER, UserRole.LOGISTICIAN)


def _service(session: SessionDep, settings: SettingsDep, dadata: DadataDep) -> BulkService:
    return BulkService(
        BulkRepository(session),
        RateShoppingService(session, settings, dadata),
        RecommendationService(session),
        DecisionService(session),
        ShipmentService(session, settings, dadata),
    )


@bulk_router.post(
    "",
    response_model=BulkRunOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать массовый расчёт",
)
async def create_run(
    payload: BulkRunCreateIn,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
) -> BulkRunOut:
    """Создать черновик: один отправитель, много получателей."""
    return await _service(session, settings, dadata).create(
        payload, tenant_id=principal.tenant_id, user_id=principal.user_id
    )


@bulk_router.post(
    "/import",
    response_model=BulkImportOut,
    summary="Разобрать список получателей и подобрать по адресной книге",
)
async def import_rows(
    payload: BulkImportIn,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
) -> BulkImportOut:
    """Предпросмотр списка: что распозналось и что нашлось в адресной книге.

    Прогон не создаётся — оператор сначала видит результат подбора и сам
    решает, что делать со строками, по которым найдено несколько адресов.
    Объявлен раньше путей с идентификатором, чтобы «import» не читался
    как номер прогона.
    """
    return await _service(session, settings, dadata).import_rows(
        payload, tenant_id=principal.tenant_id
    )


@bulk_router.get("", response_model=BulkRunPage, summary="Список массовых расчётов")
async def list_runs(
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BulkRunPage:
    return await _service(session, settings, dadata).page(
        tenant_id=principal.tenant_id, limit=limit, offset=offset
    )


@bulk_router.get("/{run_id}", response_model=BulkRunOut, summary="Массовый расчёт")
async def get_run(
    run_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
) -> BulkRunOut:
    return await _service(session, settings, dadata).get(run_id, tenant_id=principal.tenant_id)


@bulk_router.patch("/{run_id}", response_model=BulkRunOut, summary="Переименовать расчёт")
async def rename_run(
    run_id: UUID,
    payload: BulkRunRenameIn,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
) -> BulkRunOut:
    return await _service(session, settings, dadata).rename(
        run_id, payload.name, tenant_id=principal.tenant_id
    )


@bulk_router.post("/{run_id}/quote", response_model=BulkRunOut, summary="Посчитать все строки")
async def quote_run(
    run_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
) -> BulkRunOut:
    """Опросить перевозчиков по каждой строке.

    Строка, по которой расчёт не получился, гасит себя, а не прогон:
    частичный успех — нормальное состояние.
    """
    return await _service(session, settings, dadata).quote_all(
        run_id, tenant_id=principal.tenant_id, user_id=principal.user_id
    )


@bulk_router.post("/{run_id}/select", response_model=BulkRunOut, summary="Выбрать по каждой строке")
async def select_run(
    run_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
    _allowed: Annotated[object, _CAN_SHIP] = None,
) -> BulkRunOut:
    """Построить рекомендацию и принять решение по стратегии прогона."""
    return await _service(session, settings, dadata).select_all(
        run_id, tenant_id=principal.tenant_id, user_id=principal.user_id
    )


@bulk_router.post("/{run_id}/create", response_model=BulkRunOut, summary="Оформить отправления")
async def create_shipments(
    run_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    dadata: DadataDep,
    _allowed: Annotated[object, _CAN_SHIP] = None,
) -> BulkRunOut:
    """Оформить заказы у перевозчиков по всем выбранным строкам.

    Ключ идемпотентности каждой строки выводится из прогона и строки, поэтому
    повторный вызов не создаёт вторых заказов.
    """
    return await _service(session, settings, dadata).create_all(
        run_id, tenant_id=principal.tenant_id, user_id=principal.user_id
    )
