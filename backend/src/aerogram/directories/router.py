"""Эндпоинты справочников: города, адреса, терминалы, очередь сопоставления."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from aerogram.core.deps import (
    CurrentPrincipal,
    SessionDep,
    SettingsDep,
    client_ip,
    require_roles,
)
from aerogram.core.service import AuditService
from aerogram.directories.deps import DadataDep
from aerogram.directories.models import City
from aerogram.directories.repository import CarrierRepository, TerminalRepository
from aerogram.directories.schemas import (
    CarrierConnectionOut,
    CarrierHealthOut,
    CityMappingConfirm,
    CityMappingQueueItem,
    CityOut,
    CitySuggestResponse,
    NormalizedAddress,
    PartyDraft,
    PartyLookupRequest,
    TerminalListResponse,
    TerminalOut,
)
from aerogram.directories.service import (
    AddressService,
    CarrierDirectoryService,
    CityMappingService,
    CityService,
)
from aerogram.shared.enums import UserRole
from aerogram.shared.errors import NotFound, ValidationFailed

__all__ = ["admin_directories_router", "directories_router"]

directories_router = APIRouter(tags=["Справочники"])
admin_directories_router = APIRouter(prefix="/admin", tags=["Администрирование платформы"])


@directories_router.get(
    "/cities/suggest",
    response_model=CitySuggestResponse,
    summary="Подсказки города",
)
async def suggest_cities(
    principal: CurrentPrincipal,
    session: SessionDep,
    dadata: DadataDep,
    query: Annotated[str, Query(min_length=1, max_length=300, alias="q")],
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> CitySuggestResponse:
    """Подсказки населённого пункта.

    Никогда не отвечает ошибкой: при недоступности ДаData выдача собирается
    из локального справочника и помечается ``degraded``. Иначе сбой чужого
    сервиса останавливал бы создание отправления целиком.
    """
    return await CityService(session, dadata).suggest(query, limit)


@directories_router.get(
    "/cities",
    response_model=list[CityOut],
    summary="Города по идентификаторам ФИАС",
)
async def cities_by_fias(
    principal: CurrentPrincipal,
    session: SessionDep,
    fias_id: Annotated[list[str], Query(min_length=1, max_length=50)],
) -> list[City]:
    """Прочитать города по уже известным идентификаторам.

    Подсказки (``/cities/suggest``) отвечают на «какой город имеется в виду»,
    а этот путь — на «как называется тот, что уже выбран». Без него условие
    правила маршрутизации, хранящее города списком ФИАС, нечитаемо: экран мог
    бы только сосчитать, сколько их.

    Ненайденный идентификатор не ошибка, а более короткий ответ: город мог
    исчезнуть из справочника, а правило с ним продолжает существовать,
    и отказывать в показе всего правила из-за одного города незачем.
    """
    return await CityService(session, None).by_fias_ids(fias_id)


@directories_router.post(
    "/addresses/normalize",
    response_model=NormalizedAddress,
    summary="Нормализация адреса по ФИАС",
)
async def normalize_address(
    principal: CurrentPrincipal,
    session: SessionDep,
    dadata: DadataDep,
    query: Annotated[str, Query(min_length=1, max_length=300, alias="q")],
) -> NormalizedAddress:
    """Привести адрес к ФИАС и оценить пригодность к перевозке."""
    return await AddressService(session, dadata).normalize(query)


@directories_router.post(
    "/parties/lookup",
    response_model=PartyDraft,
    summary="Поиск организации по ИНН",
)
async def lookup_party(
    payload: PartyLookupRequest,
    principal: CurrentPrincipal,
    session: SessionDep,
    dadata: DadataDep,
) -> PartyDraft:
    """Черновик контрагента по ИНН для адресной книги (FR-8.4)."""
    return await AddressService(session, dadata).find_party(payload.inn, payload.kpp)


@directories_router.get(
    "/carriers",
    response_model=list[CarrierConnectionOut],
    summary="Подключённые перевозчики",
)
async def list_carriers(
    principal: CurrentPrincipal,
    session: SessionDep,
) -> list[CarrierConnectionOut]:
    """Перевозчики платформы и состояние подключения тенанта.

    Неподключённые тоже в списке: экран подключения существует затем, чтобы
    показать, кого ещё можно подключить, и что для этого потребуется ввести.

    Учётные данные не возвращаются ни в каком виде — только имена полей,
    которых требует перевозчик.
    """
    return await CarrierDirectoryService(session).connections()


@directories_router.post(
    "/carriers/{code}/check",
    response_model=CarrierHealthOut,
    summary="Проверить подключение к перевозчику",
)
async def check_carrier(
    code: str,
    principal: Annotated[object, require_roles(UserRole.OWNER, UserRole.LOGISTICIAN)],
    session: SessionDep,
    settings: SettingsDep,
) -> CarrierHealthOut:
    """Авторизованный вызов к перевозчику под доступами тенанта.

    Отвечает 200 и при неудаче: «доступы не работают» — это ответ на вопрос
    кабинета, а не сбой запроса. Ошибкой считается только то, что помешало
    спросить: неизвестный перевозчик или отсутствие подключения.

    ``POST``, а не ``GET``: проверка тратит вызов у перевозчика и записывает
    итог в учётную запись.
    """
    return await CarrierDirectoryService(session, settings).check(code)


@directories_router.get(
    "/carriers/{code}/terminals",
    response_model=TerminalListResponse,
    summary="Терминалы и пункты выдачи перевозчика",
)
async def list_terminals(
    code: str,
    principal: CurrentPrincipal,
    session: SessionDep,
    city_fias_id: Annotated[str | None, Query(min_length=36, max_length=36)] = None,
    types: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TerminalListResponse:
    """Терминалы перевозчика в городе.

    Город обязателен. Без него запрос постранично выкачивает весь справочник:
    при лимите 120 запросов в минуту (FR-10.3) и 200 строках на страницу это
    24 тысячи строк в минуту одним ключом, тогда как форма создания отправления
    город знает всегда.
    """
    if city_fias_id is None:
        raise ValidationFailed("Укажите город", field="city_fias_id")

    carrier = await CarrierRepository(session).get_by_code(code)
    if carrier is None:
        raise NotFound("Перевозчик не найден")

    items, total = await TerminalRepository(session).list_in_city(
        carrier.id,
        city_fias_id,
        types=tuple(types or ()),
        limit=limit,
        offset=offset,
    )
    return TerminalListResponse(
        items=[TerminalOut.model_validate(t) for t in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@admin_directories_router.get(
    "/city-mappings",
    response_model=list[CityMappingQueueItem],
    summary="Очередь ручного сопоставления городов",
)
async def list_city_mapping_queue(
    principal: Annotated[object, require_roles(UserRole.PLATFORM_ADMIN)],
    session: SessionDep,
    carrier_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[CityMappingQueueItem]:
    """Несопоставленные города перевозчиков (FR-12.3).

    Сверху те, на которых висит больше терминалов: их разбор даёт наибольший
    эффект на единицу внимания администратора.
    """
    from aerogram.directories.repository import CityMappingRepository

    items = await CityMappingRepository(session).list_open(carrier_id, limit)
    return [CityMappingQueueItem.model_validate(item) for item in items]


@admin_directories_router.post(
    "/city-mappings/{item_id}/confirm",
    status_code=204,
    summary="Подтвердить сопоставление города",
)
async def confirm_city_mapping(
    item_id: UUID,
    payload: CityMappingConfirm,
    request: Request,
    session: SessionDep,
    principal: Annotated[object, require_roles(UserRole.PLATFORM_ADMIN)],
) -> None:
    """Ручное сопоставление города. Решение человека старше машинного."""
    actor: CurrentPrincipal = principal  # type: ignore[assignment]
    await CityMappingService(session).confirm(item_id, payload.city_fias_id, actor.user_id)
    AuditService(session).record(
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="city_mapping.confirm",
        entity_type="city_mapping_queue",
        entity_id=item_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        payload_diff={"city_fias_id": payload.city_fias_id},
    )
