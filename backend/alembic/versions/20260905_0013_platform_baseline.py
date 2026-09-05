"""Платформенная база Carrier Score и основание оценки.

Скор перевозчика должен существовать у клиента, который этим перевозчиком
ещё не возил (ADR-0026). Для этого нужна база, посчитанная по всем клиентам
сразу, и признак того, на чьих данных стоит показанное число.

**``carrier_platform_baselines`` — единственная таблица без тенанта и без
RLS, и это не забытая политика.** Строка описывает перевозчика, а не клиента,
и показывается всем сразу. Изоляцию здесь заменяют два ограничения и одно
отсутствие:

* ``tenants_count >= 3`` — свод по одному или двум клиентам это их
  статистика, выданная остальным; при двух каждый вычитает себя и получает
  числа второго почти точно. Именно такую утечку закрывала ADR-0017,
  и порог существует затем, чтобы она не вернулась другим путём;
* ``sample_size >= 30`` — число, которое видит каждый клиент, не должно
  стоять на десяти отправлениях;
* **колонки цены нет вовсе.** Тариф — коммерческая тайна клиента,
  и усреднённый по платформе он рассказал бы соседям об уровне чужих
  договоров. Индекс цены остаётся внутри тенанта.

Пороги записаны числами прямо в ``CHECK``, а не берутся из констант
приложения: их изменение — решение о чужих данных, и оно обязано проходить
миграцией через ревью человека, а не правкой одной строки в Python.

Снапшот получает ``basis`` и ``platform_sample_size``. Существующие строки
получают ``basis = 'none'``: они посчитаны формулой 1.0.0, где платформенной
базы не было вовсе, и приписывать им другое основание значило бы задним
числом объявить, что они опирались на данные, которых не существовало.
Пересчёт версией 2.0.0 создаст новые снапшоты рядом, не переписывая эти
(FR-7.4).

Revision ID: 0013_platform_baseline
Revises: 0012_bulk_runs
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_platform_baseline"
down_revision: str | None = "0012_bulk_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Роль приложения. Берётся из окружения по образцу миграции 0003, чтобы
#: миграция не зависела от соглашения конкретного стенда.
APP_ROLE = os.getenv("APP_DB_ROLE", "aerogram_app")

_TABLE = "carrier_platform_baselines"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "carrier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("carriers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        # Сколько РАЗНЫХ клиентов дали хотя бы одно завершённое отправление.
        sa.Column("tenants_count", sa.Integer(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("on_time_rate", sa.Numeric(5, 4)),
        sa.Column("reliability", sa.Numeric(5, 4)),
        sa.Column("incident_free", sa.Numeric(5, 4)),
        sa.Column("data_quality", sa.Numeric(5, 4)),
        sa.Column("formula_version", sa.String(20), nullable=False),
        sa.Column(
            "calculated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "carrier_id",
            "period_start",
            "period_end",
            "formula_version",
            name="uq_carrier_platform_baselines_period",
        ),
        sa.CheckConstraint("tenants_count >= 3", name="platform_baseline_anonymity"),
        sa.CheckConstraint("sample_size >= 30", name="platform_baseline_sample"),
        sa.CheckConstraint("period_end >= period_start", name="platform_baseline_period_order"),
    )
    op.create_index(
        "ix_carrier_platform_baselines_lookup",
        _TABLE,
        ["carrier_id", "period_end"],
    )
    # RLS на этой таблице НЕ включается намеренно — см. строку документации.
    # Права выдаются явно, как в миграции 0009: полагаться на умолчания
    # схемы значит однажды получить работающий стенд и молчащий прод.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {APP_ROLE}")

    op.add_column(
        "carrier_score_snapshots",
        sa.Column("basis", sa.String(20), nullable=False, server_default="none"),
    )
    op.add_column(
        "carrier_score_snapshots",
        sa.Column("platform_sample_size", sa.Integer()),
    )


def downgrade() -> None:
    op.drop_column("carrier_score_snapshots", "platform_sample_size")
    op.drop_column("carrier_score_snapshots", "basis")
    op.drop_index("ix_carrier_platform_baselines_lookup", table_name=_TABLE)
    op.drop_table(_TABLE)
