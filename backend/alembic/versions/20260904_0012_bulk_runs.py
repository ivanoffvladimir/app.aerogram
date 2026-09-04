"""Массовые отправления: прогон и его строки.

Две таблицы поверх Decision Engine (ADR-0022). Ни одна не хранит того, что уже
хранят ``rate_quotes``, ``recommendations``, ``decisions`` и ``shipments``, —
только ссылается на них, поэтому пересчёт задним числом невозможен by
construction.

Отдельного признака «тариф заменён вручную» здесь нет намеренно: замена — это
``Decision`` с ``override = true``, который уже реализован и уже попадает
в метрику Override Rate.

Два ограничения строки существуют затем, чтобы состояние нельзя было записать
непоследовательно: строка со статусом ``failed`` обязана назвать причину,
а строка со статусом ``created`` обязана ссылаться на отправление. Оба случая
иначе выглядели бы в кабинете как «что-то произошло, но что — неизвестно».

RLS включается и **форсируется**: без ``FORCE`` владелец таблицы читает чужие
строки, а миграции у нас выполняются отдельной ролью.

Revision ID: 0012_bulk_runs
Revises: 0011_mfa_replay_guard
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_bulk_runs"
down_revision: str | None = "0011_mfa_replay_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "tenant_isolation"
_TENANT_PREDICATE = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {table}"
        f" USING ({_TENANT_PREDICATE}) WITH CHECK ({_TENANT_PREDICATE})"
    )


def upgrade() -> None:
    op.create_table(
        "bulk_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Внешнего ключа на tenants нет — как и у остальных бизнес-таблиц:
        # изоляция обеспечивается RLS, а не ссылочной целостностью.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'draft'")
        ),
        sa.Column("strategy", sa.String(length=20), nullable=True),
        sa.Column("sender_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_bulk_runs_tenant_id", "bulk_runs", ["tenant_id"])
    op.create_index("ix_bulk_runs_tenant_id_created_at", "bulk_runs", ["tenant_id", "created_at"])
    op.create_index("ix_bulk_runs_tenant_id_status", "bulk_runs", ["tenant_id", "status"])
    _enable_rls("bulk_runs")

    op.create_table(
        "bulk_rows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Внешнего ключа на tenants нет — как и у остальных бизнес-таблиц:
        # изоляция обеспечивается RLS, а не ссылочной целостностью.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bulk_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("recipient_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("cargo_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "rate_quote_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rate_quotes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "recommendation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("recommendations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("decisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "shipment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shipments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'new'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("run_id", "position", name="uq_bulk_rows_run_id_position"),
        sa.CheckConstraint(
            "status <> 'failed' OR error_message IS NOT NULL",
            name="failed_row_states_the_reason",
        ),
        sa.CheckConstraint(
            "status <> 'created' OR shipment_id IS NOT NULL",
            name="created_row_has_a_shipment",
        ),
    )
    op.create_index("ix_bulk_rows_tenant_id", "bulk_rows", ["tenant_id"])
    op.create_index("ix_bulk_rows_tenant_id_run_id", "bulk_rows", ["tenant_id", "run_id"])
    op.create_index("ix_bulk_rows_run_id_status", "bulk_rows", ["run_id", "status"])
    _enable_rls("bulk_rows")


def downgrade() -> None:
    op.drop_table("bulk_rows")
    op.drop_table("bulk_runs")
