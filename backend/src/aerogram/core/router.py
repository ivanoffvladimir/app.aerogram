"""HTTP-эндпоинты ядра: вход, профиль, пользователи."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request, status

from aerogram.core.deps import (
    AuthServiceDep,
    CurrentPrincipal,
    SessionDep,
    client_ip,
    require_roles,
)
from aerogram.core.schemas import (
    LoginRequest,
    RefreshRequest,
    TokenPair,
    UserCreate,
    UserOut,
)
from aerogram.core.service import AuditService, UserService
from aerogram.shared.enums import UserRole

__all__ = ["auth_router", "users_router"]

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
        role=payload.role,
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
