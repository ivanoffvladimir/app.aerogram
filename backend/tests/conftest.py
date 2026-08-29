"""Общие фикстуры тестов.

Тесты репозиториев и RLS идут против НАСТОЯЩЕГО PostgreSQL, а не против моков:
изоляцию тенантов иначе не проверить — мок подтвердит что угодно (раздел 11 ТЗ).

Адрес БД берётся из ``TEST_DATABASE_URL``. Если переменная не задана, поднимается
контейнер через testcontainers. В CI используется service-контейнер, локально —
compose из ``make up``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aerogram.config import Settings
from aerogram.core.models import Tenant, User
from aerogram.core.security import hash_password
from aerogram.db import set_tenant
from aerogram.shared.enums import TenantStatus, UserRole
from aerogram.shared.ids import uuid7

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Настройки для тестов. Секреты — заведомо тестовые."""
    return Settings(
        environment="local",
        database_url="postgresql+asyncpg://aerogram_app:app@127.0.0.1:5433/aerogram",  # type: ignore[arg-type]
        redis_url="redis://127.0.0.1:6379/0",  # type: ignore[arg-type]
        jwt_secret="test-secret-that-is-long-enough-for-validation-0123",
        credential_keys="k1:" + "A" * 43 + "=",
    )


@pytest.fixture(scope="session")
def database_url() -> str:
    """URL БД для интеграционных тестов, под ролью приложения (без BYPASSRLS)."""
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        return url
    pytest.skip("TEST_DATABASE_URL не задан: интеграционные тесты требуют настоящего PostgreSQL")


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    """Сессия под ролью приложения. Транзакция откатывается после теста."""
    engine = create_async_engine(database_url, poolclass=None)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        transaction = await db.begin()
        try:
            yield db
        finally:
            await transaction.rollback()
            await db.close()
    await engine.dispose()


@pytest.fixture
async def migrator_session(database_url: str) -> AsyncIterator[AsyncSession]:
    """Сессия под ролью миграций — для подготовки данных двух тенантов.

    Нужна отдельно: роль приложения из-за RLS не может создать строку тенанта,
    для которого ``app.tenant_id`` ещё не установлен.
    """
    url = os.getenv("TEST_MIGRATION_DATABASE_URL", database_url)
    engine = create_async_engine(url, poolclass=None)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
        await db.rollback()
    await engine.dispose()


@pytest.fixture
def tenant_a_id() -> UUID:
    return uuid7()


@pytest.fixture
def tenant_b_id() -> UUID:
    return uuid7()


def make_tenant(tenant_id: UUID, name: str) -> Tenant:
    """Тенант для тестов."""
    return Tenant(id=tenant_id, name=name, status=TenantStatus.ACTIVE, plan="pilot")


def make_user(tenant_id: UUID, email: str, role: UserRole = UserRole.LOGISTICIAN) -> User:
    """Пользователь для тестов. Пароль заведомо тестовый."""
    return User(
        id=uuid7(),
        tenant_id=tenant_id,
        email=email,
        full_name="Тестовый Пользователь",
        role=role,
        password_hash=hash_password("test-password-12345"),
        is_active=True,
    )


async def with_tenant(db: AsyncSession, tenant_id: UUID) -> None:
    """Установить тенанта на текущую транзакцию."""
    await set_tenant(db, tenant_id)


@pytest.fixture(scope="session")
def source_files() -> Iterator[list[Path]]:
    """Все исходники backend — для тестов-сторожей, читающих код проекта."""
    yield sorted((BACKEND_ROOT / "src" / "aerogram").rglob("*.py"))
