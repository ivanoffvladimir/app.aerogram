"""Репозитории ядра. Единственное место с SQL в модуле (CLAUDE.md §4, правило 5).

Все запросы к бизнес-таблицам выполняются в транзакции с установленным
``app.tenant_id``: RLS отфильтрует чужие строки на уровне БД даже при забытом WHERE.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.core.models import ApiKey, AuditLog, CarrierAccount, Tenant, User
from aerogram.shared.clock import utcnow

__all__ = ["ApiKeyRepository", "AuditRepository", "TenantRepository", "UserRepository"]


class UserRepository:
    """Доступ к пользователям."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        """Найти пользователя по e-mail в пределах текущего тенанта.

        Тенант не фигурирует в запросе явно: его подставляет RLS. Это и есть
        проверяемая гарантия изоляции — забыть её нельзя.
        """
        stmt = select(User).where(User.email == email.lower())
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_active(self) -> list[User]:
        stmt = select(User).where(User.is_active.is_(True)).order_by(User.created_at)
        return list((await self._session.execute(stmt)).scalars())

    async def touch_login(self, user_id: UUID) -> None:
        await self._session.execute(
            update(User).where(User.id == user_id).values(last_login_at=utcnow())
        )

    def add(self, user: User) -> User:
        self._session.add(user)
        return user


class TenantRepository:
    """Доступ к тенантам. Платформенная таблица: RLS на ней нет."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, tenant_id: UUID) -> Tenant | None:
        return await self._session.get(Tenant, tenant_id)

    async def get_by_inn(self, inn: str) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.inn == inn)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> list[Tenant]:
        stmt = select(Tenant).order_by(Tenant.created_at)
        return list((await self._session.execute(stmt)).scalars())

    def add(self, tenant: Tenant) -> Tenant:
        self._session.add(tenant)
        return tenant


class ApiKeyRepository:
    """Доступ к API-ключам."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        """Найти действующий ключ по хешу.

        Запрос идёт БЕЗ установленного тенанта: тенанта мы как раз и определяем
        по ключу. Поэтому таблица ``api_keys`` читается платформенной ролью через
        отдельный путь — см. ``core.service.ApiKeyAuthService``.
        """
        stmt = select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_tenant(self) -> list[ApiKey]:
        stmt = select(ApiKey).where(ApiKey.revoked_at.is_(None)).order_by(ApiKey.created_at)
        return list((await self._session.execute(stmt)).scalars())

    async def touch_used(self, key_id: UUID) -> None:
        await self._session.execute(
            update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=utcnow())
        )

    def add(self, key: ApiKey) -> ApiKey:
        self._session.add(key)
        return key


class AuditRepository:
    """Запись в аудит-лог."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def record(self, entry: AuditLog) -> AuditLog:
        self._session.add(entry)
        return entry


class CarrierAccountRepository:
    """Учётные записи тенанта у перевозчиков."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self) -> list[CarrierAccount]:
        stmt = (
            select(CarrierAccount)
            .where(CarrierAccount.is_active.is_(True))
            .order_by(CarrierAccount.created_at)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def get_by_id(self, account_id: UUID) -> CarrierAccount | None:
        return await self._session.get(CarrierAccount, account_id)

    def add(self, account: CarrierAccount) -> CarrierAccount:
        self._session.add(account)
        return account
