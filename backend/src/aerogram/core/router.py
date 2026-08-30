"""HTTP-эндпоинты ядра: вход, профиль, пользователи."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from aerogram.core.deps import (
    AuthServiceDep,
    CurrentPrincipal,
    SessionDep,
    client_ip,
    require_roles,
)
from aerogram.core.schemas import (
    AddressCreate,
    AddressOut,
    CounterpartyCreate,
    CounterpartyOut,
    LoginRequest,
    Page,
    RefreshRequest,
    TokenPair,
    UserCreate,
    UserOut,
)
from aerogram.core.service import AddressBookService, AuditService, UserService
from aerogram.shared.enums import UserRole

__all__ = ["auth_router", "counterparties_router", "users_router"]

auth_router = APIRouter(prefix="/auth", tags=["Аутентификация"])
users_router = APIRouter(prefix="/users", tags=["Пользователи"])


@auth_router.post("/login", response_model=TokenPair, summary="Вход в систему")
async def login(payload: LoginRequest, service: AuthServiceDep) -> TokenPair:
    result = await service.login(payload.email, payload.password, payload.mfa_code)
    return TokenPair(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
    )


@auth_router.post("/refresh", response_model=TokenPair, summary="Обновление токена")
async def refresh(payload: RefreshRequest, service: AuthServiceDep) -> TokenPair:
    result = await service.refresh(payload.refresh_token)
    return TokenPair(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
    )


@auth_router.get("/me", response_model=UserOut, summary="Текущий пользователь")
async def me(principal: CurrentPrincipal, session: SessionDep) -> UserOut:
    if principal.user_id is None:
        # Машинный клиент: профиля пользователя у него нет.
        return UserOut(
            id=principal.tenant_id,
            tenant_id=principal.tenant_id,
            email="api-client@aerogram.local",
            full_name="Машинный клиент",
            role=UserRole.API_CLIENT,
            is_active=True,
            mfa_enabled=False,
        )
    user = await UserService(session).get(principal.user_id)
    return UserOut.model_validate(user)


@users_router.get("", response_model=list[UserOut], summary="Пользователи компании")
async def list_users(principal: CurrentPrincipal, session: SessionDep) -> list[UserOut]:
    users = await UserService(session).list_active()
    return [UserOut.model_validate(u) for u in users]


@users_router.post(
    "",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать пользователя",
)
async def create_user(
    payload: UserCreate,
    request: Request,
    session: SessionDep,
    principal: Annotated[object, require_roles(UserRole.OWNER)],
) -> UserOut:
    actor: CurrentPrincipal = principal  # type: ignore[assignment]
    user = await UserService(session).create(
        tenant_id=actor.tenant_id,
        email=payload.email,
        full_name=payload.full_name,
        # Единственное место перехода из узкого словаря запроса в широкий
        # словарь хранения. Значения совпадают по построению, а платформенной
        # роли в TenantRole нет физически.
        role=UserRole(payload.role.value),
        password=payload.password,
    )
    AuditService(session).record(
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="user.create",
        entity_type="user",
        entity_id=user.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return UserOut.model_validate(user)


counterparties_router = APIRouter(prefix="/counterparties", tags=["Адресная книга"])


@counterparties_router.get(
    "",
    response_model=Page[CounterpartyOut],
    summary="Поиск контрагентов",
)
async def search_counterparties(
    principal: CurrentPrincipal,
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[CounterpartyOut]:
    """Контрагенты тенанта с поиском по названию и ИНН (FR-8.4).

    Строка из одних цифр трактуется как ИНН и ищется по префиксу, всё
    остальное — как подстрока названия в любом месте.
    """
    items, total = await AddressBookService(session).search(q, limit=limit, offset=offset)
    return Page[CounterpartyOut](
        items=[CounterpartyOut.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@counterparties_router.post(
    "",
    response_model=CounterpartyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Завести контрагента",
)
async def create_counterparty(
    payload: CounterpartyCreate,
    request: Request,
    session: SessionDep,
    principal: Annotated[object, require_roles(UserRole.OWNER, UserRole.LOGISTICIAN)],
) -> CounterpartyOut:
    actor: CurrentPrincipal = principal  # type: ignore[assignment]
    counterparty = await AddressBookService(session).create(
        tenant_id=actor.tenant_id,
        type_=payload.type,
        name=payload.name,
        inn=payload.inn,
        kpp=payload.kpp,
        contact_person=payload.contact_person,
        phone=payload.phone,
        email=payload.email,
        addresses=[a.model_dump() for a in payload.addresses],
    )
    AuditService(session).record(
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="counterparty.create",
        entity_type="counterparty",
        entity_id=counterparty.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return CounterpartyOut.model_validate(counterparty)


@counterparties_router.get(
    "/{counterparty_id}",
    response_model=CounterpartyOut,
    summary="Карточка контрагента",
)
async def get_counterparty(
    counterparty_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> CounterpartyOut:
    counterparty = await AddressBookService(session).get(counterparty_id)
    return CounterpartyOut.model_validate(counterparty)


@counterparties_router.post(
    "/{counterparty_id}/addresses",
    response_model=AddressOut,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить адрес контрагенту",
)
async def add_address(
    counterparty_id: UUID,
    payload: AddressCreate,
    session: SessionDep,
    principal: Annotated[object, require_roles(UserRole.OWNER, UserRole.LOGISTICIAN)],
) -> AddressOut:
    actor: CurrentPrincipal = principal  # type: ignore[assignment]
    service = AddressBookService(session)
    counterparty = await service.get(counterparty_id)
    address = await service.add_address(
        tenant_id=actor.tenant_id, counterparty=counterparty, payload=payload.model_dump()
    )
    return AddressOut.model_validate(address)


@counterparties_router.get(
    "/{counterparty_id}/addresses",
    response_model=list[AddressOut],
    summary="Адреса контрагента",
)
async def list_addresses(
    counterparty_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> list[AddressOut]:
    addresses = await AddressBookService(session).list_addresses(counterparty_id)
    return [AddressOut.model_validate(a) for a in addresses]


@counterparties_router.delete(
    "/{counterparty_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить контрагента",
)
async def delete_counterparty(
    counterparty_id: UUID,
    request: Request,
    session: SessionDep,
    principal: Annotated[object, require_roles(UserRole.OWNER, UserRole.LOGISTICIAN)],
) -> None:
    """Мягкое удаление: адреса используются в отправлениях и не исчезают."""
    actor: CurrentPrincipal = principal  # type: ignore[assignment]
    await AddressBookService(session).soft_delete(counterparty_id)
    AuditService(session).record(
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="counterparty.delete",
        entity_type="counterparty",
        entity_id=counterparty_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
