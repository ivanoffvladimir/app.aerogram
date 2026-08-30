"""Репозитории ядра. Единственное место с SQL в модуле (CLAUDE.md §4, правило 5).

Все запросы к бизнес-таблицам выполняются в транзакции с установленным
``app.tenant_id``: RLS отфильтрует чужие строки на уровне БД даже при забытом WHERE.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.core.models import (
    Address,
    ApiKey,
    AuditLog,
    CarrierAccount,
    Counterparty,
    Tenant,
    User,
)
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
        по ключу. Поэтому вызывать этот метод можно только из
        ``core.service.ApiKeyService._lookup_key``, который открывает узкое
        окно поверх RLS и закрывает его вместе с транзакцией (ADR-0004).
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


class CounterpartyRepository:
    """Контрагенты адресной книги (FR-8.4).

    Все выборки отсекают мягко удалённых: физическое удаление невозможно —
    ``shipments`` ссылается на адреса с ``ondelete="RESTRICT"``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _alive(self) -> Select[tuple[Counterparty]]:
        return select(Counterparty).where(Counterparty.deleted_at.is_(None))

    async def get_by_id(self, counterparty_id: UUID) -> Counterparty | None:
        stmt = self._alive().where(Counterparty.id == counterparty_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_inn(self, inn: str, kpp: str | None) -> Counterparty | None:
        """Найти контрагента по паре ИНН + КПП.

        Условие совпадает с уникальным индексом ``(tenant_id, inn,
        coalesce(kpp, ''))``: головная организация и её филиал — разные
        контрагенты с одним ИНН и разными КПП. Если бы отсутствие КПП
        трактовалось как «любой КПП», проверка была бы строже индекса,
        и завести головную организацию после филиала стало бы невозможно.
        """
        stmt = self._alive().where(Counterparty.inn == inn)
        stmt = stmt.where(
            Counterparty.kpp == kpp if kpp is not None else Counterparty.kpp.is_(None)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def search(
        self, query: str | None, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[Counterparty], int]:
        """Поиск по названию и ИНН.

        Поиск по названию — подстрока в любом месте: оператор набирает «плом»
        и должен увидеть «Роспломба». Это обслуживается GIN-индексом
        по триграммам; полнотекстовый поиск такой запрос не находит, потому
        что ищет по началу лексемы.
        """
        stmt = self._alive()
        if query:
            cleaned = query.strip()
            pattern = f"%{cleaned}%"
            if cleaned.isdigit():
                # ИНН — отдельная ветка: префиксный поиск по btree.
                stmt = stmt.where(Counterparty.inn.like(f"{cleaned}%"))
            else:
                stmt = stmt.where(
                    or_(
                        Counterparty.name.ilike(pattern), Counterparty.contact_person.ilike(pattern)
                    )
                )

        total = int(
            (
                await self._session.execute(select(func.count()).select_from(stmt.subquery()))
            ).scalar_one()
        )
        page = stmt.order_by(Counterparty.name, Counterparty.id).limit(limit).offset(offset)
        return list((await self._session.execute(page)).scalars()), total

    def add(self, counterparty: Counterparty) -> Counterparty:
        self._session.add(counterparty)
        return counterparty


class AddressRepository:
    """Адреса контрагентов."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, address_id: UUID) -> Address | None:
        stmt = select(Address).where(Address.id == address_id, Address.deleted_at.is_(None))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_counterparty(self, counterparty_id: UUID) -> list[Address]:
        stmt = (
            select(Address)
            .where(Address.counterparty_id == counterparty_id, Address.deleted_at.is_(None))
            .order_by(Address.is_default_sender.desc(), Address.created_at)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def clear_default_sender(self, keep_id: UUID | None = None) -> None:
        """Снять признак отправителя по умолчанию со всех адресов тенанта.

        Уникальный частичный индекс не даёт существовать двум таким адресам,
        поэтому старый снимается до установки нового, а не после.
        """
        stmt = update(Address).where(Address.is_default_sender.is_(True))
        if keep_id is not None:
            stmt = stmt.where(Address.id != keep_id)
        await self._session.execute(stmt.values(is_default_sender=False))

    def add(self, address: Address) -> Address:
        self._session.add(address)
        return address
