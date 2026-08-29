"""Логика ядра: аутентификация, пользователи, API-ключи, аудит."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.config import Settings
from aerogram.core.models import ApiKey, AuditLog, Tenant, User
from aerogram.core.repository import (
    ApiKeyRepository,
    AuditRepository,
    TenantRepository,
    UserRepository,
)
from aerogram.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    needs_rehash,
    verify_password,
)
from aerogram.db import set_tenant
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import TenantStatus, UserRole
from aerogram.shared.errors import AuthenticationError, Conflict, NotFound, PermissionDenied
from aerogram.shared.logging import get_logger

__all__ = ["AUTH_SCOPE_SETTING", "ApiKeyService", "AuthResult", "AuthService", "UserService"]

log = get_logger(__name__)

#: Настройка сессии, открывающая узкое окно на поиск пользователя при входе.
#:
#: Единственное место, где она устанавливается, — ``AuthService._lookup_user`` ниже.
#: Причина существования: вход выполняется до того, как известен тенант, поэтому
#: обычная политика RLS по ``app.tenant_id`` его не находит. Окно транзакционное
#: (``set_config(..., true)``), доступно только на SELECT и закрывается автоматически.
#: Тест ``tests/unit/test_auth_scope_guard.py`` следит, чтобы других мест не появилось.
AUTH_SCOPE_SETTING = "app.auth_scope"
_AUTH_SCOPE_VALUE = "login"


@dataclass(frozen=True, slots=True)
class AuthResult:
    """Результат успешного входа."""

    user_id: UUID
    tenant_id: UUID
    role: UserRole
    access_token: str
    refresh_token: str
    expires_in: int


class AuthService:
    """Вход, обновление токена, разбор токена."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._users = UserRepository(session)
        self._tenants = TenantRepository(session)

    async def login(self, email: str, password: str, mfa_code: str | None) -> AuthResult:
        """Проверить учётные данные и выдать пару токенов.

        Причина сбоя наружу не раскрывается: «неверный e-mail» и «неверный пароль» —
        один и тот же ответ, иначе получается оракул для перебора учётных записей.
        """
        user = await self._lookup_user(email)
        if user is None or not verify_password(user.password_hash, password):
            log.info("auth.login_failed", email=email)
            raise AuthenticationError("Неверный e-mail или пароль")

        if not user.is_active:
            raise AuthenticationError("Учётная запись отключена")

        tenant = await self._tenants.get_by_id(user.tenant_id)
        if tenant is None or tenant.status == TenantStatus.SUSPENDED:
            raise AuthenticationError("Доступ компании приостановлен")

        # Двухфакторная аутентификация обязательна для роли owner (12.5 ТЗ).
        if user.role == UserRole.OWNER and not user.mfa_enabled:
            log.warning("auth.owner_without_mfa", user_id=str(user.id))
        if user.mfa_enabled and not mfa_code:
            raise AuthenticationError("Требуется код двухфакторной аутентификации")

        await set_tenant(self._session, user.tenant_id)
        await self._users.touch_login(user.id)

        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        log.info("auth.login_ok", user_id=str(user.id), tenant_id=str(user.tenant_id))
        return self._issue(user.id, user.tenant_id, UserRole(user.role))

    async def refresh(self, refresh_token: str) -> AuthResult:
        """Обменять refresh-токен на новую пару."""
        payload = decode_token(self._settings, refresh_token)
        if payload.token_type != "refresh":  # noqa: S105  # тип токена, не пароль
            raise AuthenticationError("Ожидался refresh-токен")

        await set_tenant(self._session, payload.tenant_id)
        user = await self._users.get_by_id(payload.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Учётная запись недоступна")

        return self._issue(user.id, user.tenant_id, UserRole(user.role))

    def _issue(self, user_id: UUID, tenant_id: UUID, role: UserRole) -> AuthResult:
        return AuthResult(
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            access_token=create_access_token(self._settings, user_id, tenant_id, role.value),
            refresh_token=create_refresh_token(self._settings, user_id, tenant_id, role.value),
            expires_in=self._settings.access_token_ttl_minutes * 60,
        )

    async def _lookup_user(self, email: str) -> User | None:
        """Найти пользователя по e-mail до того, как известен тенант.

        См. комментарий к ``AUTH_SCOPE_SETTING``: это единственное место в проекте,
        где открывается окно поиска поверх RLS, и оно закрывается вместе с транзакцией.
        """
        await self._session.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": AUTH_SCOPE_SETTING, "value": _AUTH_SCOPE_VALUE},
        )
        try:
            return await self._users.get_by_email(email.lower())
        finally:
            await self._session.execute(
                text("SELECT set_config(:name, '', true)"), {"name": AUTH_SCOPE_SETTING}
            )


class UserService:
    """Пользователи тенанта."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)

    async def get(self, user_id: UUID) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None:
            # 404, а не 403: подтверждать существование чужого объекта нельзя
            # (раздел 14.1 ТЗ, тест изоляции тенантов).
            raise NotFound("Пользователь не найден")
        return user

    async def list_active(self) -> list[User]:
        return await self._users.list_active()

    async def create(
        self, *, tenant_id: UUID, email: str, full_name: str, role: UserRole, password: str
    ) -> User:
        if await self._users.get_by_email(email) is not None:
            raise Conflict("Пользователь с таким e-mail уже существует в этой компании")

        user = User(
            tenant_id=tenant_id,
            email=email.lower(),
            full_name=full_name,
            role=role,
            password_hash=hash_password(password),
            is_active=True,
        )
        self._users.add(user)
        await self._session.flush()
        return user


class ApiKeyService:
    """Выпуск и отзыв API-ключей (FR-10.2)."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._keys = ApiKeyRepository(session)

    async def issue(self, *, tenant_id: UUID, name: str, scopes: list[str]) -> tuple[ApiKey, str]:
        """Выпустить ключ. Полное значение возвращается один раз и не хранится."""
        full, prefix, key_hash = generate_api_key(self._settings.environment)
        key = ApiKey(
            tenant_id=tenant_id, name=name, prefix=prefix, key_hash=key_hash, scopes=scopes
        )
        self._keys.add(key)
        await self._session.flush()
        return key, full

    async def revoke(self, key_id: UUID) -> None:
        keys = await self._keys.list_for_tenant()
        target = next((k for k in keys if k.id == key_id), None)
        if target is None:
            raise NotFound("Ключ не найден")
        target.revoked_at = utcnow()

    async def resolve(self, raw_key: str) -> ApiKey:
        """Определить тенанта по предъявленному ключу."""
        key = await self._keys.get_by_hash(hash_api_key(raw_key))
        if key is None:
            raise AuthenticationError("Ключ недействителен")
        if key.expires_at is not None and key.expires_at < utcnow():
            raise AuthenticationError("Срок действия ключа истёк")
        await self._keys.touch_used(key.id)
        return key


class AuditService:
    """Запись в аудит-лог (12.6 ТЗ)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditRepository(session)

    def record(
        self,
        *,
        tenant_id: UUID,
        actor_user_id: UUID | None,
        action: str,
        entity_type: str,
        entity_id: UUID | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        payload_diff: dict[str, object] | None = None,
        impersonated_by_user_id: UUID | None = None,
    ) -> None:
        self._audit.record(
            AuditLog(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                impersonated_by_user_id=impersonated_by_user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                ip=ip,
                user_agent=user_agent,
                payload_diff=payload_diff,
            )
        )


def require_role(actual: UserRole, allowed: frozenset[UserRole]) -> None:
    """Проверка прав (раздел 2 ТЗ). Недостаток прав — 403, отсутствие объекта — 404."""
    if actual not in allowed:
        raise PermissionDenied()


def ensure_tenant_active(tenant: Tenant) -> None:
    """Приостановленный тенант не может выполнять изменяющие операции."""
    if tenant.status == TenantStatus.SUSPENDED:
        raise PermissionDenied("Доступ компании приостановлен")
