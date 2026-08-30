"""Carrier Score становится собственностью тенанта.

Таблица снапшотов была объявлена платформенной витриной: без ``tenant_id``,
без RLS, с уникальностью по перевозчику, разрезу, периоду и версии формулы.
Наблюдения при этом берутся из ``shipments`` и ``delivery_outcomes``, а они под
``FORCE ROW LEVEL SECURITY``: роль приложения читает только своего тенанта.
Пересчёт по расписанию обходит тенантов по одному и ПЕРЕЗАПИСЫВАЕТ одну и ту же
строку. В ней остаётся статистика того тенанта, который считался последним,
и ``GET /v1/analytics/carriers`` отдаёт её всем остальным: долю доставок в срок,
долю инцидентов, индекс цены и размер выборки чужого клиента — как свойства
перевозчика.

Решение принято человеком и записано в ADR-0017: скор свой у каждого тенанта.

Существующие строки УДАЛЯЮТСЯ, а не раздаются. Кому принадлежат наблюдения,
из которых они посчитаны, не знает никто: приписать их тенанту значило бы выдать
чужие числа за его собственные ещё раз. Ближайший пересчёт считает всё заново,
а до него экран показывает «недостаточно данных» — отсутствие числа честнее
чужого числа.

Revision ID: 0009_score_per_tenant
Revises: 0008_api_key_lookup
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_score_per_tenant"
down_revision: str | None = "0008_api_key_lookup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "carrier_score_snapshots"
_POLICY = "tenant_isolation"
_UNIQUE = "uq_carrier_score_snapshots_scope_period"
_APP_ROLE = "aerogram_app"


def upgrade() -> None:
    # Сначала удаление, потом NOT NULL: иначе колонку нечем заполнить, а
    # заполнять её и нечем — принадлежность старых строк неизвестна.
    op.execute(f"DELETE FROM {_TABLE}")

    op.add_column(_TABLE, sa.Column("tenant_id", sa.UUID(), nullable=False))
    op.create_index(op.f(f"ix_{_TABLE}_tenant_id"), _TABLE, ["tenant_id"], unique=False)

    # Ключ уникальности с тенантом — то, из-за чего пересчёт затирал чужое.
    op.drop_constraint(_UNIQUE, _TABLE, type_="unique")
    op.create_unique_constraint(
        _UNIQUE,
        _TABLE,
        [
            "tenant_id",
            "carrier_id",
            "scope_type",
            "scope_key",
            "period_start",
            "period_end",
            "formula_version",
        ],
    )

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    # FORCE обязателен: без него политика не действует на владельца таблицы,
    # и миграционная роль читала бы всё подряд.
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    # nullif(..., '') обязателен: сброшенная настройка хранится пустой строкой,
    # и ''::uuid уронил бы ЛЮБОЙ запрос ошибкой типа вместо того, чтобы просто
    # не отдать строк.
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON {_TABLE}
            USING (
                tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
            )
            WITH CHECK (
                tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
            )
        """
    )

    # Прав на таблицу у роли приложения не прибавляется: они выданы миграцией
    # 0003 на все таблицы схемы. Строка ниже — на случай базы, поднятой из
    # более позднего снимка, и ничего не меняет там, где права уже есть.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_APP_ROLE}")


def downgrade() -> None:
    # Возврат к платформенной витрине. Строки снова удаляются: с них снимается
    # принадлежность тенанту, и оставить их значило бы вернуть ту же утечку.
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {_TABLE}")
    op.execute(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"DELETE FROM {_TABLE}")

    op.drop_constraint(_UNIQUE, _TABLE, type_="unique")
    op.create_unique_constraint(
        _UNIQUE,
        _TABLE,
        [
            "carrier_id",
            "scope_type",
            "scope_key",
            "period_start",
            "period_end",
            "formula_version",
        ],
    )
    op.drop_index(op.f(f"ix_{_TABLE}_tenant_id"), table_name=_TABLE)
    op.drop_column(_TABLE, "tenant_id")
