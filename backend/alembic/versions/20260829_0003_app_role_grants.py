"""Права роли приложения.

Revision ID: 0003_app_role_grants
Revises: 0002_tenant_isolation

Роль приложения получает ровно то, что нужно для работы: чтение и запись данных,
использование последовательностей. Прав на DDL у неё нет — схему меняет только
миграционная роль (раздел 7.2 ТЗ).

Имя роли берётся из переменной окружения ``APP_DB_ROLE`` (по умолчанию
``aerogram_app``), чтобы миграция не зависела от соглашения конкретного стенда.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0003_app_role_grants"
down_revision: str | None = "0002_tenant_isolation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = os.getenv("APP_DB_ROLE", "aerogram_app")


def upgrade() -> None:
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    # Таблицы будущих миграций тоже должны быть доступны роли приложения.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE}"
    )
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
