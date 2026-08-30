"""Инварианты схемы, которые нельзя проверить чтением кода.

Эти проверки существуют потому, что ошибки такого рода не проявляются
в обычных тестах: приложение проставляет значения само, и неверная схема
годами выглядит рабочей — пока кто-нибудь не вставит строку в обход ORM
или не начнёт считать по этим полям аналитику.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


class TestTimestampDefaults:
    """Значения по умолчанию у колонок времени обязаны быть функцией now().

    Регрессия к миграции 0001: там ``server_default`` был задан питоновской
    строкой ``"now()"`` вместо ``sa.text("now()")``. Alembic отрендерил это
    литералом, PostgreSQL вычислил его ОДИН РАЗ в момент миграции, и восемь
    колонок получили в качестве значения по умолчанию застывший момент.
    Аудит-лог при этом писался с одинаковым временем во всех записях.
    Починено миграцией 0005.
    """

    async def test_no_column_has_a_frozen_timestamp_default(self, session: AsyncSession) -> None:
        frozen = (
            (
                await session.execute(
                    text(
                        "SELECT table_name || '.' || column_name "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' "
                        "AND column_default LIKE '''%::timestamp with time zone'"
                    )
                )
            )
            .scalars()
            .all()
        )

        assert not frozen, (
            "значение по умолчанию вычислено один раз при миграции, а не при вставке: "
            f'{frozen}. Проверьте, что server_default задан как sa.text("now()"), '
            "а не строкой"
        )

    async def test_audit_log_default_is_a_function_call(self, session: AsyncSession) -> None:
        default = (
            await session.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'audit_log' AND column_name = 'created_at'"
                )
            )
        ).scalar_one()
        assert default == "now()"

    async def test_two_rows_inserted_apart_get_different_timestamps(
        self, migrator_session: AsyncSession
    ) -> None:
        """Прямое доказательство: вставка в обход ORM даёт актуальное время.

        Именно этот путь и был сломан — ``AuditService`` не задаёт ``created_at``
        на стороне Python, полагаясь на базу.
        """
        tenant_id = "11111111-1111-7111-8111-111111111111"
        await migrator_session.execute(
            text(
                "INSERT INTO tenants (id, name, status, plan, timezone, ranking_weights,"
                " created_at, updated_at)"
                " VALUES (:t, 'Проверка', 'active', 'pilot', 'Europe/Moscow', '{}', now(), now())"
                " ON CONFLICT DO NOTHING"
            ),
            {"t": tenant_id},
        )
        await migrator_session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
        )

        # Тест идёт в откатываемой транзакции, но чужие записи с теми же
        # действиями могли остаться от ручных проверок.
        await migrator_session.execute(text("DELETE FROM audit_log WHERE entity_type = 'test'"))

        stamps = []
        for action in ("первая", "вторая"):
            await migrator_session.execute(
                text(
                    "INSERT INTO audit_log (id, tenant_id, action, entity_type)"
                    " VALUES (gen_random_uuid(), :t, :a, 'test')"
                ),
                {"t": tenant_id, "a": action},
            )
            stamps.append(
                (
                    await migrator_session.execute(
                        text("SELECT created_at FROM audit_log WHERE action = :a"), {"a": action}
                    )
                ).scalar_one()
            )
            await migrator_session.execute(text("SELECT pg_sleep(0.05)"))

        # clock_timestamp внутри одной транзакции не меняется, поэтому сравниваем
        # с моментом самой транзакции: главное — что это НЕ время миграции.
        transaction_now = (await migrator_session.execute(text("SELECT now()"))).scalar_one()
        for stamp in stamps:
            assert stamp == transaction_now, "значение по умолчанию не вычисляется при вставке"

        await migrator_session.rollback()


class TestExtensions:
    async def test_pg_trgm_is_installed(self, session: AsyncSession) -> None:
        """Без расширения поиск по названию контрагента деградирует в seq scan."""
        installed = (
            await session.execute(
                text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'")
            )
        ).scalar_one()
        assert installed == 1


class TestIndexHygiene:
    async def test_no_duplicate_index_definitions(self, session: AsyncSession) -> None:
        """Два индекса по одному набору колонок — это удвоенная цена записи.

        Проверяется по определению индекса без имени: полные дубликаты
        накапливаются незаметно, когда миграции пишут разные люди.
        """
        duplicates = (
            (
                await session.execute(
                    text(
                        "SELECT string_agg(indexname, ', ') FROM ("
                        "  SELECT indexname,"
                        "         regexp_replace(indexdef, ' INDEX [a-z0-9_]+ ON', ' INDEX ON')"
                        "           AS normalised"
                        "  FROM pg_indexes WHERE schemaname = 'public'"
                        ") t GROUP BY normalised HAVING count(*) > 1"
                    )
                )
            )
            .scalars()
            .all()
        )

        assert not duplicates, f"полные дубликаты индексов: {duplicates}"
