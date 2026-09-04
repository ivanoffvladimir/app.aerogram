"""Окружение Alembic.

Миграции выполняются под ОТДЕЛЬНОЙ ролью (``DATABASE_MIGRATION_URL``), у которой есть
права на DDL. Роль приложения таких прав не имеет и не имеет ``BYPASSRLS``
(раздел 7.2 ТЗ).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from aerogram.config import get_settings

# Импорт моделей нужен ради заполнения Base.metadata — без него autogenerate
# сочтёт, что таблиц нет, и сгенерирует пустую миграцию.
from aerogram.bulk import models as bulk_models  # noqa: F401
from aerogram.core import models as core_models  # noqa: F401
from aerogram.db import Base
from aerogram.directories import models as directories_models  # noqa: F401
from aerogram.documents import models as documents_models  # noqa: F401
from aerogram.intelligence import models as intelligence_models  # noqa: F401
from aerogram.rating import models as rating_models  # noqa: F401
from aerogram.routing import models as routing_models  # noqa: F401
from aerogram.shipments import models as shipments_models  # noqa: F401
from aerogram.tracking import models as tracking_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    settings = get_settings()
    url = settings.database_migration_url or settings.database_url
    return str(url)


def run_migrations_offline() -> None:
    """Сгенерировать SQL без подключения к БД."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Применить миграции к БД."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
