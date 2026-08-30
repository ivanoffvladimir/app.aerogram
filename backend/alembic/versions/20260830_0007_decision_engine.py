"""Сущности Decision Engine (ТЗ v3, ADR-0014).

Две части.

**Первая — переименование по контракту.** В `docs/tz/v3/openapi.yaml` и в ЭРД
«quote» — это снимок запроса расчёта, а отдельные предложения перевозчиков
называются «offers». У нас исторически было наоборот: `rate_requests` хранил
запрос, а `rate_quotes` — предложения. Оставить как есть значило бы держать
перевод между кодом и контрактом в голове у каждого, кто читает и то и другое.

    rate_quotes    → rate_offers     (предложение перевозчика)
    rate_requests  → rate_quotes     (снимок запроса)

Порядок важен: сначала освобождается имя `rate_quotes`, потом занимается.

**Вторая — новые сущности.** `cost_components`, `recommendations`, `decisions`,
`delivery_outcomes`, `routing_rules`. Все получают RLS: без неё новая таблица
с `tenant_id` открыта всем тенантам, и это молчаливая ошибка.

Неизменяемость. Снимки `rate_offers`, `recommendations` и `decisions` после
принятия решения не пересчитываются (продуктовое ТЗ, раздел 8). На уровне схемы
это выражено отсутствием у `decisions` колонки `updated_at` и запретом менять
`recommendation_id`/`selected_offer_id` — за последним следит триггер.

Revision ID: 0007_decision_engine
Revises: 0006_money_minor_units
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_decision_engine"
down_revision: str | None = "0006_money_minor_units"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Новые таблицы с tenant_id. Каждая получает RLS — как в миграции 0002.
NEW_TENANT_TABLES: tuple[str, ...] = (
    "cost_components",
    "recommendations",
    "decisions",
    "delivery_outcomes",
    "routing_rules",
)

_POLICY = "tenant_isolation"
_TENANT_PREDICATE = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"

#: Старое имя индекса → новое. PostgreSQL не переименовывает индексы вслед
#: за таблицей, и без этого на rate_offers остались бы имена ix_rate_quotes_*.
_INDEX_RENAMES: tuple[tuple[str, str], ...] = (
    ("ix_rate_quotes_tenant_id", "ix_rate_offers_tenant_id"),
    ("ix_rate_requests_tenant_id", "ix_rate_quotes_tenant_id"),
    ("ix_rate_quotes_rate_request_id", "ix_rate_offers_quote_id"),
    ("ix_rate_quotes_tenant_id_created_at", "ix_rate_offers_tenant_id_created_at"),
    ("ix_rate_requests_tenant_id_created_at", "ix_rate_quotes_tenant_id_created_at"),
    ("ix_rate_requests_tenant_id_hash", "ix_rate_quotes_tenant_id_hash"),
)

_CONSTRAINT_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("rate_offers", "ck_rate_quotes_quote_price_xor_error", "ck_rate_offers_total_xor_error"),
    ("rate_offers", "ck_rate_quotes_quote_price_non_negative", "ck_rate_offers_total_non_negative"),
    ("rate_offers", "ck_rate_quotes_currency_is_iso_4217", "ck_rate_offers_currency_is_iso_4217"),
)


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {table}"
        f" USING ({_TENANT_PREDICATE}) WITH CHECK ({_TENANT_PREDICATE})"
    )


def upgrade() -> None:
    _rename_quote_tables()
    _extend_rate_quotes()
    _extend_rate_offers()
    _create_cost_components()
    _create_recommendations()
    _create_decisions()
    _create_delivery_outcomes()
    _create_routing_rules()
    _link_shipments_to_decisions()

    for table in NEW_TENANT_TABLES:
        # Соглашение схемы: индекс по tenant_id и БЕЗ внешнего ключа на tenants —
        # так устроены все 16 таблиц из миграции 0001. Вопрос о добавлении FK
        # всем таблицам вынесен в docs/status.md, менять его здесь в одиночку
        # значило бы завести в одной схеме две договорённости.
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        _enable_rls(table)


def _rename_quote_tables() -> None:
    # Внешний ключ отправления указывал на выбранное ПРЕДЛОЖЕНИЕ, а не на запрос,
    # поэтому вслед за таблицей переименовывается и колонка.
    op.execute("ALTER TABLE shipments RENAME COLUMN rate_quote_id TO rate_offer_id")
    op.execute("ALTER TABLE rate_quotes RENAME TO rate_offers")
    op.execute("ALTER TABLE rate_requests RENAME TO rate_quotes")
    op.execute("ALTER TABLE rate_offers RENAME COLUMN rate_request_id TO quote_id")
    op.execute("ALTER TABLE rate_offers RENAME COLUMN price_amount_minor TO total_amount_minor")
    op.execute("ALTER TABLE rate_quotes RENAME COLUMN payload TO input_snapshot")
    op.execute("ALTER TABLE rate_quotes RENAME COLUMN expires_at TO valid_until")
    # У предложения свой срок жизни, отдельный от срока жизни запроса.
    op.execute("ALTER TABLE rate_offers RENAME COLUMN expires_at TO valid_until")

    for old, new in _INDEX_RENAMES:
        op.execute(f"ALTER INDEX {old} RENAME TO {new}")
    for table, old, new in _CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new}")


def _extend_rate_quotes() -> None:
    """Снимок запроса: стратегия и дедлайн — часть входных данных решения."""
    op.add_column("rate_quotes", sa.Column("strategy", sa.String(20), nullable=True))
    op.add_column("rate_quotes", sa.Column("deadline", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "rate_quotes",
        sa.Column(
            "no_deadline_match", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.create_index("ix_rate_quotes_valid_until", "rate_quotes", ["valid_until"])


def _extend_rate_offers() -> None:
    """Предложение: пригодность, срок и показатели надёжности."""
    op.add_column("rate_offers", sa.Column("source", sa.String(20), nullable=True))
    op.add_column("rate_offers", sa.Column("eta", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "rate_offers",
        sa.Column("eligible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column("rate_offers", sa.Column("ineligibility_reason", sa.String(40), nullable=True))
    op.add_column(
        "rate_offers", sa.Column("deadline_margin_seconds", sa.BigInteger(), nullable=True)
    )
    op.add_column("rate_offers", sa.Column("lateness_seconds", sa.BigInteger(), nullable=True))
    op.add_column(
        "rate_offers",
        sa.Column("on_time_probability", sa.Numeric(precision=4, scale=3), nullable=True),
    )
    op.add_column("rate_offers", sa.Column("probability_label", sa.String(10), nullable=True))
    op.add_column("rate_offers", sa.Column("risk", sa.String(10), nullable=True))
    op.create_check_constraint(
        "on_time_probability_is_a_probability",
        "rate_offers",
        "on_time_probability IS NULL OR (on_time_probability >= 0 AND on_time_probability <= 1)",
    )
    # Непригодное предложение обязано называть причину: строка без причины
    # выглядит в интерфейсе как необъяснимо отключённая.
    op.create_check_constraint(
        "ineligible_offer_states_the_reason",
        "rate_offers",
        "eligible OR ineligibility_reason IS NOT NULL",
    )
    op.create_index("ix_rate_offers_quote_id_eligible", "rate_offers", ["quote_id", "eligible"])


def _create_cost_components() -> None:
    """Составляющие Total Cost: база, страхование, надбавки."""
    op.create_table(
        "cost_components",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("offer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        # Ставка процента — не деньги, поэтому NUMERIC: 0.18 означает 0.18 %.
        sa.Column("rate_percent", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["offer_id"], ["rate_offers.id"], ondelete="CASCADE"),
        sa.CheckConstraint("amount_minor >= 0", name="component_amount_non_negative"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_is_iso_4217"),
        sa.CheckConstraint(
            "rate_percent IS NULL OR rate_percent >= 0", name="component_rate_non_negative"
        ),
    )
    op.create_index("ix_cost_components_offer_id", "cost_components", ["offer_id"])


def _create_recommendations() -> None:
    """Рекомендация: что система предложила и на каком основании."""
    op.create_table(
        "recommendations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recommended_offer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("strategy", sa.String(20), nullable=False),
        # Факты объяснения, а не готовый текст: локализация — забота интерфейса
        # (системное ТЗ, раздел 9).
        sa.Column("explanation", postgresql.JSONB(), nullable=False),
        sa.Column("alternatives_delta", postgresql.JSONB(), nullable=True),
        sa.Column("algorithm_version", sa.String(40), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("confidence", sa.String(10), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["quote_id"], ["rate_quotes.id"], ondelete="CASCADE"),
        # RESTRICT, а не CASCADE: удаление предложения не должно стирать историю
        # того, что оно было рекомендовано.
        sa.ForeignKeyConstraint(["recommended_offer_id"], ["rate_offers.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_recommendations_tenant_id_created_at", "recommendations", ["tenant_id", "created_at"]
    )
    op.create_index("ix_recommendations_quote_id", "recommendations", ["quote_id"])


def _create_decisions() -> None:
    """Решение: что человек или правило выбрали и почему."""
    op.create_table(
        "decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recommendation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selected_offer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("override", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("override_reason", sa.String(30), nullable=True),
        sa.Column("override_comment", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["recommendation_id"], ["recommendations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["selected_offer_id"], ["rate_offers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        # Override обязан назвать причину: без неё Override Rate не разложить
        # по причинам, а ради этого поле и существует.
        sa.CheckConstraint(
            "NOT override OR override_reason IS NOT NULL", name="override_states_the_reason"
        ),
        # Ручное решение имеет автора; автоматическое — не обязано.
        sa.CheckConstraint(
            "mode <> 'manual' OR actor_id IS NOT NULL", name="manual_decision_has_an_actor"
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_decisions_tenant_id_idempotency_key"
        ),
    )
    op.create_index("ix_decisions_tenant_id_decided_at", "decisions", ["tenant_id", "decided_at"])
    op.create_index("ix_decisions_recommendation_id", "decisions", ["recommendation_id"])

    # Неизменяемость снимка решения (продуктовое ТЗ, раздел 8). Проверка на уровне
    # БД, а не приложения: приложение можно обойти скриптом, триггер нельзя.
    op.execute(
        """
        CREATE FUNCTION decisions_are_immutable() RETURNS trigger AS $$
        BEGIN
            IF NEW.recommendation_id IS DISTINCT FROM OLD.recommendation_id
               OR NEW.selected_offer_id IS DISTINCT FROM OLD.selected_offer_id
               OR NEW.mode IS DISTINCT FROM OLD.mode
               OR NEW.override IS DISTINCT FROM OLD.override
               OR NEW.decided_at IS DISTINCT FROM OLD.decided_at THEN
                RAISE EXCEPTION
                    'снимок решения неизменяем: правка решения % запрещена', OLD.id;
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER decisions_immutable BEFORE UPDATE ON decisions"
        " FOR EACH ROW EXECUTE FUNCTION decisions_are_immutable()"
    )


def _create_delivery_outcomes() -> None:
    """Факт доставки: то, ради чего собирается вся история решений."""
    op.create_table(
        "delivery_outcomes",
        sa.Column("shipment_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_met", sa.Boolean(), nullable=True),
        sa.Column("delay_seconds", sa.BigInteger(), nullable=True),
        # Фактическая стоимость приходит из счёта и может появиться сильно позже
        # доставки (системное ТЗ, раздел 10), поэтому отдельно от факта доставки.
        sa.Column("actual_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("damage", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("claim", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("claim_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "actual_amount_minor IS NULL OR actual_amount_minor >= 0",
            name="actual_cost_non_negative",
        ),
        sa.CheckConstraint(
            "actual_amount_minor IS NULL OR currency IS NOT NULL",
            name="actual_cost_has_a_currency",
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="currency_is_iso_4217"
        ),
    )
    op.create_index(
        "ix_delivery_outcomes_tenant_id_delivered_at",
        "delivery_outcomes",
        ["tenant_id", "delivered_at"],
    )


def _create_routing_rules() -> None:
    """Правила маршрутизации: whitelist/blacklist, пороги, автовыбор."""
    op.create_table(
        "routing_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("conditions", postgresql.JSONB(), nullable=False),
        sa.Column("actions", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("policy_version", sa.String(40), nullable=False),
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
        sa.CheckConstraint("priority >= 0", name="priority_non_negative"),
        # Порядок применения правил обязан быть однозначным: два правила с одним
        # приоритетом дали бы разный результат при разном порядке чтения строк.
        sa.UniqueConstraint("tenant_id", "priority", name="uq_routing_rules_tenant_id_priority"),
    )
    op.create_index(
        "ix_routing_rules_tenant_id_enabled_priority",
        "routing_rules",
        ["tenant_id", "enabled", "priority"],
    )


def _link_shipments_to_decisions() -> None:
    """Отправление создаётся из решения (ТЗ v3, ``CreateShipmentRequest``)."""
    op.add_column(
        "shipments", sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_shipments_decision_id_decisions",
        "shipments",
        "decisions",
        ["decision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_shipments_decision_id", "shipments", ["decision_id"])


def downgrade() -> None:
    op.drop_index("ix_shipments_decision_id", table_name="shipments")
    op.drop_constraint("fk_shipments_decision_id_decisions", "shipments", type_="foreignkey")
    op.drop_column("shipments", "decision_id")

    for table in reversed(NEW_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {table}")
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)

    op.drop_table("routing_rules")
    op.drop_table("delivery_outcomes")
    op.execute("DROP TRIGGER IF EXISTS decisions_immutable ON decisions")
    op.drop_table("decisions")
    op.execute("DROP FUNCTION IF EXISTS decisions_are_immutable()")
    op.drop_table("recommendations")
    op.drop_table("cost_components")

    op.drop_index("ix_rate_offers_quote_id_eligible", table_name="rate_offers")
    op.drop_constraint(
        op.f("ck_rate_offers_ineligible_offer_states_the_reason"), "rate_offers", type_="check"
    )
    op.drop_constraint(
        op.f("ck_rate_offers_on_time_probability_is_a_probability"), "rate_offers", type_="check"
    )
    for column in (
        "risk",
        "probability_label",
        "on_time_probability",
        "lateness_seconds",
        "deadline_margin_seconds",
        "ineligibility_reason",
        "eligible",
        "eta",
        "source",
    ):
        op.drop_column("rate_offers", column)

    op.drop_index("ix_rate_quotes_valid_until", table_name="rate_quotes")
    for column in ("no_deadline_match", "deadline", "strategy"):
        op.drop_column("rate_quotes", column)

    for table, old, new in _CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old}")
    # В обратном порядке по той же причине, что и таблицы: имена меняются местами,
    # и прямой проход упёрся бы в ещё не освобождённое ix_rate_quotes_*.
    for old, new in reversed(_INDEX_RENAMES):
        op.execute(f"ALTER INDEX {new} RENAME TO {old}")

    op.execute("ALTER TABLE rate_offers RENAME COLUMN valid_until TO expires_at")
    op.execute("ALTER TABLE rate_quotes RENAME COLUMN valid_until TO expires_at")
    op.execute("ALTER TABLE rate_quotes RENAME COLUMN input_snapshot TO payload")
    op.execute("ALTER TABLE rate_offers RENAME COLUMN total_amount_minor TO price_amount_minor")
    op.execute("ALTER TABLE rate_offers RENAME COLUMN quote_id TO rate_request_id")
    op.execute("ALTER TABLE rate_quotes RENAME TO rate_requests")
    op.execute("ALTER TABLE rate_offers RENAME TO rate_quotes")
    op.execute("ALTER TABLE shipments RENAME COLUMN rate_offer_id TO rate_quote_id")
