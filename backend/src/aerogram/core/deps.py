"""Зависимости FastAPI: сессия с установленным тенантом, текущий пользователь, права."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.config import Settings, get_settings
from aerogram.core.scopes import MACHINE_SCOPES, WITHOUT_SCOPE, route_key
from aerogram.core.security import decode_token
from aerogram.core.service import ApiKeyService, AuthService, UserService
from aerogram.db import get_sessionmaker, set_tenant
from aerogram.shared.enums import UserRole
from aerogram.shared.errors import AuthenticationError, PermissionDenied
from aerogram.shared.logging import get_logger, tenant_id_var

log = get_logger(__name__)

__all__ = [
    "CurrentPrincipal",
    "Principal",
    "SessionDep",
    "SettingsDep",
    "get_session",
    "require_roles",
]


def _settings() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(_settings)]


async def get_session() -> AsyncIterator[AsyncSession]:
    """Сессия БД в открытой транзакции.

    Тенант ставится позже, зависимостью ``current_principal``: до аутентификации
    он неизвестен, а RLS без него не пропускает ничего — это и есть желаемое поведение.
    """
    factory = get_sessionmaker()
    async with factory() as session, session.begin():
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


@dataclass(frozen=True, slots=True)
class Principal:
    """Кто выполняет запрос: пользователь кабинета или машинный клиент."""

    user_id: UUID | None
    tenant_id: UUID
    role: UserRole
    via_api_key: bool = False
    scopes: tuple[str, ...] = ()


async def current_principal(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
) -> Principal:
    """Определить субъекта запроса и установить тенанта на транзакцию.

    Порядок важен: сначала мы узнаём тенанта, потом ставим ``app.tenant_id``. Всё,
    что выполняется до этого момента, RLS не пропускает.
    """
    if x_api_key:
        keys = ApiKeyService(session, settings)
        key = await keys.resolve(x_api_key)
        await set_tenant(session, key.tenant_id)
        tenant_id_var.set(str(key.tenant_id))
        # Отметка использования — уже после установки тенанта: это запись
        # в таблицу под RLS, и до set_tenant она молча не находила строк.
        await keys.mark_used(key.id)
        _authorize_machine(request, tuple(key.scopes))
        return Principal(
            user_id=None,
            tenant_id=key.tenant_id,
            role=UserRole.API_CLIENT,
            via_api_key=True,
            scopes=tuple(key.scopes),
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError()

    payload = decode_token(settings, authorization.split(" ", 1)[1].strip())
    if payload.token_type != "access":  # noqa: S105  # тип токена, не пароль
        raise AuthenticationError("Ожидался access-токен")

    await set_tenant(session, payload.tenant_id)
    tenant_id_var.set(str(payload.tenant_id))

    # Токен подписан нами, но пользователь мог быть отключён после его выдачи.
    user = await UserService(session).get(payload.user_id)
    if not user.is_active:
        raise AuthenticationError("Учётная запись отключена")

    return Principal(user_id=user.id, tenant_id=user.tenant_id, role=UserRole(user.role))


def _authorize_machine(request: Request, scopes: tuple[str, ...]) -> None:
    """Проверить, что ключу этот путь разрешён (``core.scopes``).

    Проверка стоит здесь, а не на каждом эндпоинте, ровно по той причине,
    по которой таблица одна: забытая на новом пути проверка не видна никак,
    путь просто работает шире обещанного. Умолчание — запрет.

    Отказ — 403, а не 404: ключ действителен, тенант известен, и скрывать
    существование пути не от кого. Скрывают чужой объект, а не собственный
    недостаток прав.
    """
    route = request.scope.get("route")
    key = route_key(request.method, getattr(route, "path_format", None) or request.url.path)

    if key in WITHOUT_SCOPE:
        return
    required = MACHINE_SCOPES.get(key)
    if required is None:
        # Сюда попадают и кабинетные пути, и любой новый: и то и другое
        # закрыто, пока кто-то не решит иначе и не впишет в таблицу.
        log.info("auth.scope_denied", path=key[1], method=key[0], reason="not_for_machines")
        raise PermissionDenied("Этот путь недоступен по API-ключу")
    if required not in scopes:
        log.info("auth.scope_denied", path=key[1], method=key[0], reason="scope_missing")
        raise PermissionDenied(f"Ключу не выдана область {required}")


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_roles(*roles: UserRole) -> object:
    """Фабрика зависимости проверки прав (раздел 2.1 ТЗ)."""
    allowed = frozenset(roles)

    async def _check(principal: CurrentPrincipal) -> Principal:
        if principal.role not in allowed:
            raise PermissionDenied()
        return principal

    return Depends(_check)


def client_ip(request: Request) -> str | None:
    """IP клиента с учётом обратного прокси Caddy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def auth_service(session: SessionDep, settings: SettingsDep) -> AuthService:
    return AuthService(session, settings)


AuthServiceDep = Annotated[AuthService, Depends(auth_service)]
