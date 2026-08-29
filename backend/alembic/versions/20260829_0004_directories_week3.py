"""Справочники и адресная книга: неделя 3.

Revision ID: 0004_directories_week3
Revises: 0003_app_role_grants

ОДНА ревизия на всю неделю. Разбивать её на несколько параллельных нельзя:
Alembic допускает только одну голову, а четыре ревизии с общим предком дают
«Multiple head revisions are present» и роняют `alembic upgrade head` в CI
раньше любых тестов.

Черновик получен автогенерацией и проверен построчно (раздел 7.4 ТЗ, п. 1).
Что автогенерация НЕ знает и что дописано руками:

* ``addresses.fitness`` объявлена NOT NULL — на таблице с данными это
  невозможно без server_default, поэтому default задан на уровне БД;
* расширение ``pg_trgm`` и индексы поиска по названию контрагента: поиск
  обязан находить подстроку в СЕРЕДИНЕ слова («плом» → «Роспломба»), а
  полнотекстовый поиск ищет по началу лексемы и такой запрос не находит;
* частичные уникальные индексы под мягкое удаление: удалённый контрагент
  не должен занимать ИНН, а отправитель по умолчанию должен быть один;
* индекс по ИНН с varchar_pattern_ops — под префиксный поиск.

ВНИМАНИЕ (CLAUDE.md §7): схема БД требует построчного ревью человека.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_directories_week3"
down_revision: str | None = "0003_app_role_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "city_mapping_queue",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("carrier_id", sa.UUID(), nullable=False),
        sa.Column("carrier_city_code", sa.String(length=50), nullable=False),
        sa.Column("carrier_city_name", sa.String(length=255), nullable=True),
        sa.Column("carrier_region_name", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column(
            "candidates",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("best_score", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("terminals_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_user_id", sa.UUID(), nullable=True),
        sa.Column("resolved_city_fias_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('no_match', 'ambiguous', 'conflict')",
            name=op.f("ck_city_mapping_queue_city_mapping_queue_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["carrier_id"],
            ["carriers.id"],
            name=op.f("fk_city_mapping_queue_carrier_id_carriers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_city_mapping_queue")),
        sa.UniqueConstraint(
            "carrier_id", "carrier_city_code", name="uq_city_mapping_queue_carrier_id_code"
        ),
    )
    op.create_index(
        "ix_city_mapping_queue_open",
        "city_mapping_queue",
        ["carrier_id", "terminals_count"],
        unique=False,
    )
    op.add_column(
        "addresses", sa.Column("city_parent_fias_id", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "addresses",
        sa.Column("fitness", sa.String(length=20), server_default="unusable", nullable=False),
    )
    op.add_column(
        "addresses", sa.Column("normalized_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "carrier_terminals", sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("cities", sa.Column("full_name", sa.String(length=500), nullable=True))
    op.add_column("cities", sa.Column("fias_level", sa.SmallInteger(), nullable=True))
    op.add_column("cities", sa.Column("parent_fias_id", sa.String(length=36), nullable=True))
    op.create_index("ix_cities_parent_fias_id", "cities", ["parent_fias_id"], unique=False)
    op.add_column(
        "city_carrier_map", sa.Column("match_method", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "city_carrier_map",
        sa.Column("match_score", sa.Numeric(precision=4, scale=3), nullable=True),
    )

    # --- Поиск по адресной книге (FR-8.4) ---------------------------------
    # pg_trgm — доверенное расширение с PostgreSQL 13, права владельца БД
    # достаточно, суперпользователь не нужен.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Автоподстановка обязана находить подстроку в середине слова: оператор
    # набирает «плом» и должен увидеть «Роспломба». GIN по триграммам — то,
    # что умеет ILIKE '%…%'; tsvector этот запрос не обслуживает.
    op.execute(
        "CREATE INDEX ix_counterparties_name_trgm ON counterparties "
        "USING gin (name gin_trgm_ops) WHERE deleted_at IS NULL"
    )
    # Префиксный поиск по ИНН: btree с varchar_pattern_ops обслуживает LIKE 'x%'.
    op.execute(
        "CREATE INDEX ix_counterparties_inn_prefix ON counterparties "
        "(inn varchar_pattern_ops) WHERE deleted_at IS NULL AND inn IS NOT NULL"
    )

    # --- Мягкое удаление и отправитель по умолчанию ------------------------
    # Удалённый контрагент не должен занимать ИНН: уникальность действует
    # только среди живых строк.
    op.execute(
        "CREATE UNIQUE INDEX uq_counterparties_tenant_id_inn_kpp ON counterparties "
        "(tenant_id, inn, coalesce(kpp, '')) WHERE deleted_at IS NULL AND inn IS NOT NULL"
    )
    # Отправитель по умолчанию у тенанта ровно один. Гонка двух операторов
    # ловится базой и превращается обработчиком в 409, а не в тихую порчу данных.
    op.execute(
        "CREATE UNIQUE INDEX uq_addresses_tenant_id_default_sender ON addresses "
        "(tenant_id) WHERE is_default_sender AND deleted_at IS NULL"
    )
    # Списки адресной книги всегда фильтруют удалённые.
    op.execute(
        "CREATE INDEX ix_counterparties_tenant_id_alive ON counterparties "
        "(tenant_id, name) WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_counterparties_tenant_id_alive")
    op.execute("DROP INDEX IF EXISTS uq_addresses_tenant_id_default_sender")
    op.execute("DROP INDEX IF EXISTS uq_counterparties_tenant_id_inn_kpp")
    op.execute("DROP INDEX IF EXISTS ix_counterparties_inn_prefix")
    op.execute("DROP INDEX IF EXISTS ix_counterparties_name_trgm")
    # Расширение намеренно НЕ удаляется: им могут пользоваться другие объекты,
    # а его удаление в downgrade сломало бы их без предупреждения.

    op.drop_column("city_carrier_map", "match_score")
    op.drop_column("city_carrier_map", "match_method")
    op.drop_index("ix_cities_parent_fias_id", table_name="cities")
    op.drop_column("cities", "parent_fias_id")
    op.drop_column("cities", "fias_level")
    op.drop_column("cities", "full_name")
    op.drop_column("carrier_terminals", "deactivated_at")
    op.drop_column("addresses", "normalized_at")
    op.drop_column("addresses", "fitness")
    op.drop_column("addresses", "city_parent_fias_id")
    op.drop_index("ix_city_mapping_queue_open", table_name="city_mapping_queue")
    op.drop_table("city_mapping_queue")
