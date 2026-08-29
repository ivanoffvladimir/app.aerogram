"""Движок, сессии и установка тенанта в транзакции.

Изоляция тенантов реализована в PostgreSQL (RLS), а не в приложении — это
единственная защита, которую нельзя обойти забытым WHERE (раздел 7.2 ТЗ).
Приложение обязано начинать транзакцию с установки ``app.tenant_id``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Date, DateTime, MetaData, Numeric, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aerogram.config import Settings, get_settings
from aerogram.shared.clock import utcnow
from aerogram.shared.ids import uuid7

__all__ = [
    "TENANT_SETTING",
    "Base",
    "TenantMixin",
    "TimestampMixin",
    "create_engine",
    "get_engine",
    "get_sessionmaker",
    "reset_tenant",
    "session_scope",
    "set_tenant",
    "uuid_pk",
]

#: Имя настройки сессии PostgreSQL, по которой работают все политики RLS.
TENANT_SETTING = "app.tenant_id"

#: Единые правила именования ограничений — иначе Alembic генерирует безымянные
#: индексы, и откат миграции превращается в ручную работу.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Базовый класс моделей.

    ``type_annotation_map`` фиксирует общие для проекта соответствия типов:
    деньги — NUMERIC(12,2), время — TIMESTAMPTZ, сырьё от ТК — JSONB.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012  # контракт SQLAlchemy, не изменяемое состояние
        Decimal: Numeric(12, 2),
        datetime: DateTime(timezone=True),
        date: Date(),
        UUID: PG_UUID(as_uuid=True),
        dict[str, Any]: JSONB,
    }


class TimestampMixin:
    """created_at / updated_at, всегда TIMESTAMPTZ в UTC."""

    created_at: Mapped[datetime] = mapped_column(
        default=utcnow, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=text("now()"), nullable=False
    )


class TenantMixin:
    """Обязательная колонка тенанта у каждой бизнес-таблицы (раздел 4.4 ТЗ)."""

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)


def uuid_pk() -> Mapped[UUID]:
    """Первичный ключ UUIDv7 (ADR-0003)."""
    return mapped_column(primary_key=True, default=uuid7)


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Создать движок под роль приложения (без BYPASSRLS)."""
    cfg = settings or get_settings()
    return create_async_engine(
        str(cfg.database_url),
        echo=cfg.db_echo,
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
        pool_pre_ping=True,
    )


def get_engine() -> AsyncEngine:
    """Движок процесса."""
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий процесса."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
    return _sessionmaker


async def set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    """Установить тенанта на текущую транзакцию.

    ``set_config(..., is_local => true)`` действует до конца транзакции и сбрасывается
    сам — в отличие от ``SET``, значение которого пережило бы возврат соединения в пул
    и утекло бы в следующий запрос другого тенанта.
    """
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": TENANT_SETTING, "value": str(tenant_id)},
    )


async def reset_tenant(session: AsyncSession) -> None:
    """Снять тенанта — для платформенных операций вне контекста клиента."""
    await session.execute(text("SELECT set_config(:name, '', true)"), {"name": TENANT_SETTING})


@asynccontextmanager
async def session_scope(tenant_id: UUID | None = None) -> AsyncIterator[AsyncSession]:
    """Транзакция с установленным тенантом.

    Используется в фоновых задачах и скриптах. В HTTP-обработчиках сессия приходит
    зависимостью ``core.deps.get_session``, которая делает то же самое.
    """
    factory = get_sessionmaker()
    async with factory() as session, session.begin():
        if tenant_id is not None:
            await set_tenant(session, tenant_id)
        yield session
