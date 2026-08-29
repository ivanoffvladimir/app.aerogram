"""Изоляция тенантов на уровне PostgreSQL.

Обязательный тест раздела 7.2 ТЗ и критерий приёмки 14.2, п. 11: попытка чтения
строки чужого тенанта по прямому идентификатору возвращает пустой результат
на уровне БД, а API отвечает 404, а не 403.

Тест идёт против настоящего PostgreSQL под ролью приложения. С моками он бы
подтвердил что угодно.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.core.models import Counterparty, Tenant, User
from aerogram.db import Base, reset_tenant, set_tenant
from aerogram.directories import models as _directories_models  # noqa: F401
from aerogram.documents import models as _documents_models  # noqa: F401
from aerogram.rating import models as _rating_models  # noqa: F401
from aerogram.scoring import models as _scoring_models  # noqa: F401
from aerogram.shared.enums import TenantStatus
from aerogram.shared.ids import uuid7
from aerogram.shipments import models as _shipments_models  # noqa: F401
from aerogram.tracking import models as _tracking_models  # noqa: F401
from tests.conftest import make_user

pytestmark = pytest.mark.integration


@pytest.fixture
async def two_tenants(session: AsyncSession) -> tuple[UUID, UUID, UUID, UUID]:
    """Два тенанта, у каждого — по пользователю и контрагенту.

    Возвращает ``(tenant_a, tenant_b, counterparty_a, counterparty_b)``.
    """
    tenant_a, tenant_b = uuid7(), uuid7()

    # Таблица тенантов платформенная, RLS на ней нет.
    session.add_all(
        [
            Tenant(id=tenant_a, name="Роспломба", status=TenantStatus.ACTIVE, plan="pilot"),
            Tenant(id=tenant_b, name="Конкурент", status=TenantStatus.ACTIVE, plan="pilot"),
        ]
    )
    await session.flush()

    counterparties: dict[UUID, UUID] = {}
    for tenant_id, label in ((tenant_a, "А"), (tenant_b, "Б")):
        await set_tenant(session, tenant_id)
        session.add(make_user(tenant_id, f"user-{label}@example.test"))
        counterparty = Counterparty(
            tenant_id=tenant_id, type="legal", name=f"ООО «Получатель {label}»"
        )
        session.add(counterparty)
        await session.flush()
        counterparties[tenant_id] = counterparty.id

    return tenant_a, tenant_b, counterparties[tenant_a], counterparties[tenant_b]


class TestReadIsolation:
    async def test_sees_only_own_rows(
        self, session: AsyncSession, two_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        tenant_a, tenant_b, _, _ = two_tenants

        await set_tenant(session, tenant_a)
        names_a = set((await session.execute(select(Counterparty.name))).scalars())

        await set_tenant(session, tenant_b)
        names_b = set((await session.execute(select(Counterparty.name))).scalars())

        assert "ООО «Получатель А»" in names_a
        assert "ООО «Получатель Б»" not in names_a
        assert "ООО «Получатель Б»" in names_b
        assert "ООО «Получатель А»" not in names_b

    async def test_direct_id_lookup_of_foreign_row_returns_nothing(
        self, session: AsyncSession, two_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        """Знание идентификатора не даёт доступа.

        Это тот самый случай из критериев приёмки: клиент подставляет чужой id
        и обязан получить пустоту на уровне БД, а не строку и не ошибку прав.
        """
        tenant_a, _, _, counterparty_b = two_tenants

        await set_tenant(session, tenant_a)
        stmt = select(Counterparty).where(Counterparty.id == counterparty_b)
        assert (await session.execute(stmt)).scalar_one_or_none() is None

    async def test_forgotten_where_clause_is_still_isolated(
        self, session: AsyncSession, two_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        """Смысл RLS: запрос без предиката по тенанту всё равно изолирован.

        Именно эту ошибку невозможно поймать ревью — её ловит база.
        """
        tenant_a, _, _, _ = two_tenants

        await set_tenant(session, tenant_a)
        rows = (await session.execute(select(Counterparty))).scalars().all()
        assert {row.tenant_id for row in rows} == {tenant_a}

    async def test_aggregate_does_not_leak_foreign_rows(
        self, session: AsyncSession, two_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        """Агрегаты тоже под политикой: COUNT не должен считать чужие строки."""
        tenant_a, _, _, _ = two_tenants

        await set_tenant(session, tenant_a)
        own = (await session.execute(select(func.count()).select_from(Counterparty))).scalar_one()
        assert own == 1

    async def test_no_tenant_set_returns_nothing(
        self, session: AsyncSession, two_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        """Без установленного тенанта не видно ничего.

        Это поведение по умолчанию: до аутентификации запрос не должен возвращать
        данные, даже если код случайно выполнит его раньше времени.
        """
        await reset_tenant(session)
        rows = (await session.execute(select(Counterparty))).scalars().all()
        assert rows == []


class TestWriteIsolation:
    async def test_cannot_insert_row_for_another_tenant(
        self, session: AsyncSession, two_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        """WITH CHECK не даёт записать строку под чужим тенантом."""
        tenant_a, tenant_b, _, _ = two_tenants

        await set_tenant(session, tenant_a)
        # Точка сохранения: неудачная вставка не должна ронять транзакцию теста.
        savepoint = await session.begin_nested()
        session.add(Counterparty(tenant_id=tenant_b, type="legal", name="Подложный"))
        with pytest.raises(DBAPIError):
            await session.flush()
        await savepoint.rollback()

    async def test_cannot_update_foreign_row(
        self, session: AsyncSession, two_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        """UPDATE по чужому идентификатору не затрагивает ни одной строки."""
        tenant_a, _, _, counterparty_b = two_tenants

        await set_tenant(session, tenant_a)
        result = await session.execute(
            text("UPDATE counterparties SET name = 'взломано' WHERE id = :id"),
            {"id": str(counterparty_b)},
        )
        assert result.rowcount == 0

    async def test_cannot_delete_foreign_row(
        self, session: AsyncSession, two_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        tenant_a, _, _, counterparty_b = two_tenants

        await set_tenant(session, tenant_a)
        result = await session.execute(
            text("DELETE FROM counterparties WHERE id = :id"), {"id": str(counterparty_b)}
        )
        assert result.rowcount == 0


class TestDatabaseRole:
    async def test_application_role_has_no_bypassrls(self, session: AsyncSession) -> None:
        """Роль приложения не должна иметь BYPASSRLS (раздел 7.2 ТЗ).

        С этим атрибутом все политики выше становятся декорацией.
        """
        bypasses = (
            await session.execute(
                text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).scalar_one()
        assert bypasses is False

    async def test_application_role_is_not_superuser(self, session: AsyncSession) -> None:
        is_super = (
            await session.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
        ).scalar_one()
        assert is_super is False

    async def test_every_tenant_table_has_rls_forced(self, session: AsyncSession) -> None:
        """RLS включена и принудительна на всех бизнес-таблицах.

        Забыть новую таблицу в миграции легко, обнаружить утечку потом — дорого.
        """
        # Список выводится из моделей, а не из миграции: так тест поймает таблицу,
        # которую добавили в код и забыли закрыть политикой.
        expected = sorted(
            name for name, table in Base.metadata.tables.items() if "tenant_id" in table.c
        )
        assert expected, "не найдено ни одной бизнес-таблицы с tenant_id"

        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names)"
                ),
                {"names": expected},
            )
        ).all()

        found = {row[0]: (row[1], row[2]) for row in rows}
        missing = set(expected) - set(found)
        assert not missing, f"таблицы отсутствуют в БД: {missing}"

        without_rls = [name for name, (enabled, _) in found.items() if not enabled]
        assert not without_rls, f"RLS не включена: {without_rls}"

        without_force = [name for name, (_, forced) in found.items() if not forced]
        assert not without_force, f"RLS не принудительна: {without_force}"


class TestAuthLookupWindow:
    async def test_login_window_is_scoped_to_users_only(
        self, session: AsyncSession, two_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        """Окно входа открывает только таблицу users, и только на чтение.

        Если бы оно открывало что-то ещё, компромисс из ADR-0004 был бы неприемлем.
        """
        await reset_tenant(session)
        await session.execute(text("SELECT set_config('app.auth_scope', 'login', true)"))

        users = (await session.execute(select(User))).scalars().all()
        assert len(users) >= 2, "поиск пользователя при входе должен видеть всех тенантов"

        # Всё остальное по-прежнему закрыто.
        counterparties = (await session.execute(select(Counterparty))).scalars().all()
        assert counterparties == []

    async def test_window_does_not_allow_writes(
        self, session: AsyncSession, two_tenants: tuple[UUID, UUID, UUID, UUID]
    ) -> None:
        _, tenant_b, _, _ = two_tenants
        await reset_tenant(session)
        await session.execute(text("SELECT set_config('app.auth_scope', 'login', true)"))

        result = await session.execute(
            text("UPDATE users SET full_name = 'взломано' WHERE tenant_id = :t"),
            {"t": str(tenant_b)},
        )
        assert result.rowcount == 0


class TestDatabaseLocale:
    """Локаль базы данных: без неё поиск по-русски молча не работает.

    В локали ``C`` PostgreSQL не сворачивает регистр кириллицы: ``lower('РОМАШКА')``
    возвращает строку без изменений, а ``ILIKE '%РОМАШКА%'`` не находит
    «Ромашка». Ошибка не проявляется ни в одном тесте на латинице и обнаруживается
    только жалобой оператора, у которого «не ищется контрагент».

    Требование к развёртыванию: база создаётся с UTF-8-локалью
    (``LOCALE 'C.UTF-8'``), см. deploy/init-db.sql.
    """

    async def test_database_folds_cyrillic_case(self, session: AsyncSession) -> None:
        folded = (await session.execute(text("SELECT lower('РОМАШКА')"))).scalar_one()
        assert folded == "ромашка", (
            "база создана с локалью, не сворачивающей регистр кириллицы — "
            "поиск по названию контрагента работать не будет"
        )

    async def test_case_insensitive_like_matches_cyrillic(self, session: AsyncSession) -> None:
        matched = (
            await session.execute(text("SELECT 'ООО «Ромашка»' ILIKE '%РОМАШКА%'"))
        ).scalar_one()
        assert matched is True

    async def test_database_encoding_is_utf8(self, session: AsyncSession) -> None:
        encoding = (
            await session.execute(
                text(
                    "SELECT pg_encoding_to_char(encoding) FROM pg_database "
                    "WHERE datname = current_database()"
                )
            )
        ).scalar_one()
        assert encoding == "UTF8"
