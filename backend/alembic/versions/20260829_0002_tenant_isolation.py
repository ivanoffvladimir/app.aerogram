"""Изоляция тенантов: Row Level Security.

Revision ID: 0002_tenant_isolation
Revises: 0001_initial

Изоляция реализуется на уровне PostgreSQL, а не приложения (раздел 7.2 ТЗ): это
единственная защита, которую нельзя случайно обойти забытым WHERE.

Что здесь происходит:

1. На каждой бизнес-таблице включается RLS и ``FORCE ROW LEVEL SECURITY`` — политика
   действует и на владельца таблицы, иначе миграционная роль читала бы всё подряд.
2. Политика ``tenant_isolation`` пропускает только строки текущего тенанта, взятого
   из настройки сессии ``app.tenant_id``. Незаданная или сброшенная настройка не
   пропускает ничего: ``nullif(current_setting(...), '')`` даёт NULL, сравнение даёт
   NULL, строка не проходит. Это и есть желаемое поведение до аутентификации.
3. Отдельная политика ``users_auth_lookup`` открывает УЗКОЕ окно на чтение таблицы
   ``users`` в момент входа, когда тенант ещё неизвестен. Окно транзакционное
   (``set_config(..., is_local => true)``), только на SELECT, и открывается
   единственным местом в коде — ``core.service.AuthService._lookup_user``.
   За тем, что других мест не появилось, следит тест
   ``tests/unit/test_auth_scope_guard.py``. Обоснование — ADR-0004.

Платформенные справочники (``tenants``, ``carriers``, ``carrier_services``,
``carrier_terminals``, ``cities``, ``city_carrier_map``, ``carrier_score_snapshots``)
не содержат ``tenant_id`` и RLS не получают: они общие для всех тенантов.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_tenant_isolation"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Бизнес-таблицы с колонкой tenant_id. Каждая получает RLS.
TENANT_TABLES: tuple[str, ...] = (
    "users",
    "api_keys",
    "audit_log",
    "counterparties",
    "addresses",
    "carrier_accounts",
    "carrier_raw_calls",
    "rate_requests",
    "rate_quotes",
    "shipments",
    "shipment_places",
    "shipment_items",
    "shipment_events",
    "documents",
    "webhook_subscriptions",
    "webhook_deliveries",
)

_POLICY = "tenant_isolation"
_AUTH_POLICY = "users_auth_lookup"


def upgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # nullif(..., '') обязателен: сброшенная настройка хранится как пустая строка,
        # и прямое приведение ''::uuid роняет ЛЮБОЙ запрос ошибкой типа вместо того,
        # чтобы просто не отдать строк. Через nullif предикат даёт NULL — строка
        # не проходит, и это ровно то поведение, которое нужно до аутентификации.
        op.execute(
            f"""
            CREATE POLICY {_POLICY} ON {table}
                USING (
                    tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
                )
                WITH CHECK (
                    tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
                )
            """
        )

    # Узкое окно на поиск пользователя при входе, до того как известен тенант.
    op.execute(
        f"""
        CREATE POLICY {_AUTH_POLICY} ON users
            FOR SELECT
            USING (current_setting('app.auth_scope', true) = 'login')
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_AUTH_POLICY} ON users")
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
