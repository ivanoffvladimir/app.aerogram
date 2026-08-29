"""Первичная схема Aerogram Logistic OS.

Revision ID: 0001_initial
Revises: —

Черновик получен автогенерацией и проверен построчно (раздел 7.4 ТЗ, п. 1):
сверены типы денег (NUMERIC(12,2)), timezone-aware время, частичные индексы по
незавершённым отправлениям и уникальность (carrier_id, external_id).

Изоляция тенантов (RLS) вынесена в отдельную миграцию 0002 — так критичная для
безопасности часть читается человеком отдельно от 25 таблиц схемы.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=255), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index(op.f("ix_api_keys_tenant_id"), "api_keys", ["tenant_id"], unique=False)
    op.create_index(
        "ix_api_keys_tenant_id_revoked_at", "api_keys", ["tenant_id", "revoked_at"], unique=False
    )
    op.create_table(
        "carrier_raw_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("carrier_code", sa.String(length=30), nullable=False),
        sa.Column("operation", sa.String(length=30), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("shipment_id", sa.UUID(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("is_error", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default="now()", nullable=False),
        sa.Column("expires_at", sa.Date(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_carrier_raw_calls")),
    )
    op.create_index(
        "ix_carrier_raw_calls_expires_at", "carrier_raw_calls", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_carrier_raw_calls_shipment_id", "carrier_raw_calls", ["shipment_id"], unique=False
    )
    op.create_index(
        op.f("ix_carrier_raw_calls_tenant_id"), "carrier_raw_calls", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_carrier_raw_calls_tenant_id_created_at",
        "carrier_raw_calls",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "carriers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("logo_url", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("volumetric_divisor", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_carriers")),
        sa.UniqueConstraint("code", name="uq_carriers_code"),
    )
    op.create_table(
        "cities",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("fias_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=255), nullable=True),
        sa.Column("region_fias_id", sa.String(length=36), nullable=True),
        sa.Column("kladr_id", sa.String(length=19), nullable=True),
        sa.Column("postal_code", sa.String(length=10), nullable=True),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("population", sa.Integer(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cities")),
        sa.UniqueConstraint("fias_id", name="uq_cities_fias_id"),
    )
    op.create_index("ix_cities_kladr_id", "cities", ["kladr_id"], unique=False)
    op.create_index("ix_cities_name", "cities", ["name"], unique=False)
    op.create_table(
        "counterparties",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column("kpp", sa.String(length=9), nullable=True),
        sa.Column("contact_person", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
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
            "type IN ('legal', 'individual', 'entrepreneur')",
            name=op.f("ck_counterparties_counterparty_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_counterparties")),
    )
    op.create_index(
        op.f("ix_counterparties_tenant_id"), "counterparties", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_counterparties_tenant_id_inn", "counterparties", ["tenant_id", "inn"], unique=False
    )
    op.create_index(
        "ix_counterparties_tenant_id_name", "counterparties", ["tenant_id", "name"], unique=False
    )
    op.create_table(
        "rate_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default="now()", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_requests")),
    )
    op.create_index(
        op.f("ix_rate_requests_tenant_id"), "rate_requests", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_rate_requests_tenant_id_created_at",
        "rate_requests",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_rate_requests_tenant_id_hash", "rate_requests", ["tenant_id", "hash"], unique=False
    )
    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column("kpp", sa.String(length=9), nullable=True),
        sa.Column("legal_address", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("plan", sa.String(length=50), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "ranking_weights",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default='{"price": 0.4, "transit": 0.3, "score": 0.3}',
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("inn", name="uq_tenants_inn"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mfa_secret", sa.String(length=255), nullable=True),
        sa.Column("mfa_enabled", sa.Boolean(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
    )
    op.create_index("ix_users_email_lower", "users", ["email"], unique=False)
    op.create_index(op.f("ix_users_tenant_id"), "users", ["tenant_id"], unique=False)
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("events", postgresql.ARRAY(sa.String(length=50)), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_subscriptions")),
    )
    op.create_index(
        op.f("ix_webhook_subscriptions_tenant_id"),
        "webhook_subscriptions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_webhook_subscriptions_tenant_id_is_active",
        "webhook_subscriptions",
        ["tenant_id", "is_active"],
        unique=False,
    )
    op.create_table(
        "addresses",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("counterparty_id", sa.UUID(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("region", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=255), nullable=False),
        sa.Column("city_fias_id", sa.String(length=36), nullable=True),
        sa.Column("postal_code", sa.String(length=10), nullable=True),
        sa.Column("street", sa.String(length=255), nullable=True),
        sa.Column("house", sa.String(length=50), nullable=True),
        sa.Column("flat", sa.String(length=50), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("is_default_sender", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["counterparty_id"],
            ["counterparties.id"],
            name=op.f("fk_addresses_counterparty_id_counterparties"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_addresses")),
    )
    op.create_index("ix_addresses_city_fias_id", "addresses", ["city_fias_id"], unique=False)
    op.create_index(op.f("ix_addresses_tenant_id"), "addresses", ["tenant_id"], unique=False)
    op.create_index(
        "ix_addresses_tenant_id_counterparty_id",
        "addresses",
        ["tenant_id", "counterparty_id"],
        unique=False,
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("impersonated_by_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("payload_diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default="now()", nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_log_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(
        "ix_audit_log_entity_type_entity_id",
        "audit_log",
        ["entity_type", "entity_id"],
        unique=False,
    )
    op.create_index(op.f("ix_audit_log_tenant_id"), "audit_log", ["tenant_id"], unique=False)
    op.create_index(
        "ix_audit_log_tenant_id_created_at", "audit_log", ["tenant_id", "created_at"], unique=False
    )
    op.create_table(
        "carrier_accounts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("carrier_id", sa.UUID(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("contract_number", sa.String(length=100), nullable=True),
        sa.Column("is_sandbox", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "settings", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
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
            "mode IN ('own_contract', 'aerogram')",
            name=op.f("ck_carrier_accounts_carrier_account_mode"),
        ),
        sa.ForeignKeyConstraint(
            ["carrier_id"],
            ["carriers.id"],
            name=op.f("fk_carrier_accounts_carrier_id_carriers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_carrier_accounts")),
        sa.UniqueConstraint(
            "tenant_id", "carrier_id", "mode", name="uq_carrier_accounts_tenant_id_carrier_id_mode"
        ),
    )
    op.create_index(
        op.f("ix_carrier_accounts_tenant_id"), "carrier_accounts", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_carrier_accounts_tenant_id_is_active",
        "carrier_accounts",
        ["tenant_id", "is_active"],
        unique=False,
    )
    op.create_table(
        "carrier_score_snapshots",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("carrier_id", sa.UUID(), nullable=False),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_key", sa.String(length=120), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("on_time_rate", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("avg_delay_days", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("reliability", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("incident_rate", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("price_index", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("data_quality", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.Column("formula_version", sa.String(length=20), nullable=False),
        sa.Column(
            "calculated_at", sa.DateTime(timezone=True), server_default="now()", nullable=False
        ),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_carrier_score_snapshots_score_period_order")
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)",
            name=op.f("ck_carrier_score_snapshots_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["carrier_id"],
            ["carriers.id"],
            name=op.f("fk_carrier_score_snapshots_carrier_id_carriers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_carrier_score_snapshots")),
        sa.UniqueConstraint(
            "carrier_id",
            "scope_type",
            "scope_key",
            "period_start",
            "period_end",
            "formula_version",
            name="uq_carrier_score_snapshots_scope_period",
        ),
    )
    op.create_index(
        "ix_carrier_score_snapshots_lookup",
        "carrier_score_snapshots",
        ["carrier_id", "scope_type", "scope_key", "period_end"],
        unique=False,
    )
    op.create_table(
        "carrier_services",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("carrier_id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("is_express", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
            "mode IN ('door_door', 'door_terminal', 'terminal_door', 'terminal_terminal')",
            name=op.f("ck_carrier_services_carrier_service_mode"),
        ),
        sa.ForeignKeyConstraint(
            ["carrier_id"],
            ["carriers.id"],
            name=op.f("fk_carrier_services_carrier_id_carriers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_carrier_services")),
        sa.UniqueConstraint("carrier_id", "code", name="uq_carrier_services_carrier_id_code"),
    )
    op.create_table(
        "carrier_terminals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("carrier_id", sa.UUID(), nullable=False),
        sa.Column("external_code", sa.String(length=50), nullable=False),
        sa.Column("city_fias_id", sa.String(length=36), nullable=True),
        sa.Column("city_name", sa.String(length=255), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("work_hours", sa.String(length=255), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("has_cash", sa.Boolean(), nullable=False),
        sa.Column("has_card", sa.Boolean(), nullable=False),
        sa.Column("max_weight_kg", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
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
            "type IN ('pvz', 'terminal', 'postamat')",
            name=op.f("ck_carrier_terminals_carrier_terminal_type"),
        ),
        sa.ForeignKeyConstraint(
            ["carrier_id"],
            ["carriers.id"],
            name=op.f("fk_carrier_terminals_carrier_id_carriers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_carrier_terminals")),
        sa.UniqueConstraint(
            "carrier_id", "external_code", name="uq_carrier_terminals_carrier_id_external_code"
        ),
    )
    op.create_index(
        "ix_carrier_terminals_city_fias_id_carrier_id",
        "carrier_terminals",
        ["city_fias_id", "carrier_id"],
        unique=False,
    )
    op.create_table(
        "city_carrier_map",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("city_fias_id", sa.String(length=36), nullable=False),
        sa.Column("carrier_id", sa.UUID(), nullable=False),
        sa.Column("carrier_city_code", sa.String(length=50), nullable=False),
        sa.Column("carrier_city_name", sa.String(length=255), nullable=True),
        sa.Column("is_confirmed", sa.Boolean(), nullable=False),
        sa.Column("confirmed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["carrier_id"],
            ["carriers.id"],
            name=op.f("fk_city_carrier_map_carrier_id_carriers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_city_carrier_map")),
        sa.UniqueConstraint(
            "carrier_id", "city_fias_id", name="uq_city_carrier_map_carrier_id_city_fias_id"
        ),
    )
    op.create_index(
        "ix_city_carrier_map_carrier_id_carrier_city_code",
        "city_carrier_map",
        ["carrier_id", "carrier_city_code"],
        unique=False,
    )
    op.create_index(
        "ix_city_carrier_map_is_confirmed", "city_carrier_map", ["is_confirmed"], unique=False
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("shipment_id", sa.UUID(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default="now()", nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["webhook_subscriptions.id"],
            name=op.f("fk_webhook_deliveries_subscription_id_webhook_subscriptions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
    )
    op.create_index(
        "ix_webhook_deliveries_next_attempt_at",
        "webhook_deliveries",
        ["next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_webhook_deliveries_subscription_id_created_at",
        "webhook_deliveries",
        ["subscription_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_webhook_deliveries_tenant_id"), "webhook_deliveries", ["tenant_id"], unique=False
    )
    op.create_table(
        "rate_quotes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("rate_request_id", sa.UUID(), nullable=False),
        sa.Column("carrier_id", sa.UUID(), nullable=False),
        sa.Column("carrier_account_id", sa.UUID(), nullable=True),
        sa.Column("service_code", sa.String(length=50), nullable=True),
        sa.Column("tariff_code", sa.String(length=50), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("price_source", sa.String(length=20), nullable=True),
        sa.Column("transit_days_min", sa.Integer(), nullable=True),
        sa.Column("transit_days_max", sa.Integer(), nullable=True),
        sa.Column("promised_delivery_date", sa.Date(), nullable=True),
        sa.Column("score_at_quote", sa.Integer(), nullable=True),
        sa.Column("score_confidence", sa.String(length=20), nullable=True),
        sa.Column("score_scope", sa.String(length=20), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("meets_deadline", sa.Boolean(), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default="now()", nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "(price IS NOT NULL AND error_code IS NULL) OR (price IS NULL AND error_code IS NOT NULL)",
            name=op.f("ck_rate_quotes_quote_price_xor_error"),
        ),
        sa.CheckConstraint(
            "price IS NULL OR price >= 0", name=op.f("ck_rate_quotes_quote_price_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["carrier_account_id"],
            ["carrier_accounts.id"],
            name=op.f("fk_rate_quotes_carrier_account_id_carrier_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["carrier_id"],
            ["carriers.id"],
            name=op.f("fk_rate_quotes_carrier_id_carriers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rate_request_id"],
            ["rate_requests.id"],
            name=op.f("fk_rate_quotes_rate_request_id_rate_requests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_quotes")),
    )
    op.create_index(
        "ix_rate_quotes_rate_request_id", "rate_quotes", ["rate_request_id"], unique=False
    )
    op.create_index(op.f("ix_rate_quotes_tenant_id"), "rate_quotes", ["tenant_id"], unique=False)
    op.create_index(
        "ix_rate_quotes_tenant_id_created_at",
        "rate_quotes",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "shipments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("number", sa.String(length=30), nullable=False),
        sa.Column("carrier_id", sa.UUID(), nullable=False),
        sa.Column("carrier_account_id", sa.UUID(), nullable=True),
        sa.Column("external_id", sa.String(length=100), nullable=True),
        sa.Column("tracking_number", sa.String(length=100), nullable=True),
        sa.Column("service_code", sa.String(length=50), nullable=True),
        sa.Column("tariff_code", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("carrier_status_raw", sa.String(length=255), nullable=True),
        sa.Column("sender_address_id", sa.UUID(), nullable=True),
        sa.Column("recipient_address_id", sa.UUID(), nullable=True),
        sa.Column("sender_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recipient_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("declared_value", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payment_type", sa.String(length=20), nullable=False),
        sa.Column("price_quoted", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("price_actual", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("promised_delivery_date", sa.Date(), nullable=True),
        sa.Column("actual_delivery_date", sa.Date(), nullable=True),
        sa.Column("picked_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transit_days_planned", sa.Integer(), nullable=True),
        sa.Column("transit_days_actual", sa.Integer(), nullable=True),
        sa.Column("is_late", sa.Boolean(), nullable=True),
        sa.Column("delay_days", sa.Integer(), nullable=True),
        sa.Column("has_incident", sa.Boolean(), nullable=False),
        sa.Column("incident_type", sa.String(length=50), nullable=True),
        sa.Column("created_via", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("rate_quote_id", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
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
            "price_quoted IS NULL OR price_quoted >= 0",
            name=op.f("ck_shipments_shipment_price_quoted_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["carrier_account_id"],
            ["carrier_accounts.id"],
            name=op.f("fk_shipments_carrier_account_id_carrier_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["carrier_id"],
            ["carriers.id"],
            name=op.f("fk_shipments_carrier_id_carriers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rate_quote_id"],
            ["rate_quotes.id"],
            name=op.f("fk_shipments_rate_quote_id_rate_quotes"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_address_id"],
            ["addresses.id"],
            name=op.f("fk_shipments_recipient_address_id_addresses"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sender_address_id"],
            ["addresses.id"],
            name=op.f("fk_shipments_sender_address_id_addresses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipments")),
        sa.UniqueConstraint(
            "carrier_id", "external_id", name="uq_shipments_carrier_id_external_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_shipments_tenant_id_idempotency_key"
        ),
        sa.UniqueConstraint("tenant_id", "number", name="uq_shipments_tenant_id_number"),
    )
    op.create_index(
        "ix_shipments_active_next_poll",
        "shipments",
        ["next_poll_at"],
        unique=False,
        postgresql_where=sa.text("status NOT IN ('DELIVERED', 'RETURNED', 'CANCELLED')"),
    )
    op.create_index(op.f("ix_shipments_tenant_id"), "shipments", ["tenant_id"], unique=False)
    op.create_index(
        "ix_shipments_tenant_id_created_at", "shipments", ["tenant_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_shipments_tenant_id_status_active",
        "shipments",
        ["tenant_id", "status"],
        unique=False,
        postgresql_where=sa.text("status NOT IN ('DELIVERED', 'RETURNED', 'CANCELLED')"),
    )
    op.create_index("ix_shipments_tracking_number", "shipments", ["tracking_number"], unique=False)
    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("shipment_id", sa.UUID(), nullable=True),
        sa.Column("shipment_ids", postgresql.ARRAY(sa.String(length=36)), nullable=True),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("format", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("s3_key", sa.String(length=500), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default="now()", nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name=op.f("ck_documents_document_status")
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_documents_shipment_id_shipments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(
        "ix_documents_shipment_id_type", "documents", ["shipment_id", "type"], unique=False
    )
    op.create_index(op.f("ix_documents_tenant_id"), "documents", ["tenant_id"], unique=False)
    op.create_index(
        "ix_documents_tenant_id_created_at", "documents", ["tenant_id", "created_at"], unique=False
    )
    op.create_table(
        "shipment_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("shipment_id", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default="now()", nullable=False
        ),
        sa.Column("status_normalized", sa.String(length=30), nullable=False),
        sa.Column("status_raw", sa.String(length=255), nullable=False),
        sa.Column("is_unmapped", sa.Boolean(), nullable=False),
        sa.Column("city", sa.String(length=255), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("dedup_key", sa.String(length=128), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "source IN ('api_poll', 'webhook', 'manual', 'sensor')",
            name=op.f("ck_shipment_events_event_source"),
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_shipment_events_shipment_id_shipments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipment_events")),
        sa.UniqueConstraint(
            "shipment_id", "dedup_key", name="uq_shipment_events_shipment_id_dedup_key"
        ),
    )
    op.create_index(
        "ix_shipment_events_shipment_id_occurred_at",
        "shipment_events",
        ["shipment_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shipment_events_tenant_id"), "shipment_events", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_shipment_events_tenant_id_received_at",
        "shipment_events",
        ["tenant_id", "received_at"],
        unique=False,
    )
    op.create_table(
        "shipment_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("shipment_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("weight_kg", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("nds_rate", sa.Integer(), nullable=True),
        sa.Column("marking_code", sa.String(length=255), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("price >= 0", name=op.f("ck_shipment_items_item_price_non_negative")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_shipment_items_item_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_shipment_items_shipment_id_shipments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipment_items")),
    )
    op.create_index(
        "ix_shipment_items_shipment_id", "shipment_items", ["shipment_id"], unique=False
    )
    op.create_index(
        op.f("ix_shipment_items_tenant_id"), "shipment_items", ["tenant_id"], unique=False
    )
    op.create_table(
        "shipment_places",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("shipment_id", sa.UUID(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("weight_kg", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("length_cm", sa.Integer(), nullable=False),
        sa.Column("width_cm", sa.Integer(), nullable=False),
        sa.Column("height_cm", sa.Integer(), nullable=False),
        sa.Column("volume_weight_kg", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("barcode", sa.String(length=100), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "length_cm > 0 AND width_cm > 0 AND height_cm > 0",
            name=op.f("ck_shipment_places_place_dimensions_positive"),
        ),
        sa.CheckConstraint("weight_kg > 0", name=op.f("ck_shipment_places_place_weight_positive")),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_shipment_places_shipment_id_shipments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipment_places")),
        sa.UniqueConstraint("shipment_id", "number", name="uq_shipment_places_shipment_id_number"),
    )
    op.create_index(
        op.f("ix_shipment_places_tenant_id"), "shipment_places", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_shipment_places_tenant_id"), table_name="shipment_places")
    op.drop_table("shipment_places")
    op.drop_index(op.f("ix_shipment_items_tenant_id"), table_name="shipment_items")
    op.drop_index("ix_shipment_items_shipment_id", table_name="shipment_items")
    op.drop_table("shipment_items")
    op.drop_index("ix_shipment_events_tenant_id_received_at", table_name="shipment_events")
    op.drop_index(op.f("ix_shipment_events_tenant_id"), table_name="shipment_events")
    op.drop_index("ix_shipment_events_shipment_id_occurred_at", table_name="shipment_events")
    op.drop_table("shipment_events")
    op.drop_index("ix_documents_tenant_id_created_at", table_name="documents")
    op.drop_index(op.f("ix_documents_tenant_id"), table_name="documents")
    op.drop_index("ix_documents_shipment_id_type", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_shipments_tracking_number", table_name="shipments")
    op.drop_index(
        "ix_shipments_tenant_id_status_active",
        table_name="shipments",
        postgresql_where=sa.text("status NOT IN ('DELIVERED', 'RETURNED', 'CANCELLED')"),
    )
    op.drop_index("ix_shipments_tenant_id_created_at", table_name="shipments")
    op.drop_index(op.f("ix_shipments_tenant_id"), table_name="shipments")
    op.drop_index(
        "ix_shipments_active_next_poll",
        table_name="shipments",
        postgresql_where=sa.text("status NOT IN ('DELIVERED', 'RETURNED', 'CANCELLED')"),
    )
    op.drop_table("shipments")
    op.drop_index("ix_rate_quotes_tenant_id_created_at", table_name="rate_quotes")
    op.drop_index(op.f("ix_rate_quotes_tenant_id"), table_name="rate_quotes")
    op.drop_index("ix_rate_quotes_rate_request_id", table_name="rate_quotes")
    op.drop_table("rate_quotes")
    op.drop_index(op.f("ix_webhook_deliveries_tenant_id"), table_name="webhook_deliveries")
    op.drop_index(
        "ix_webhook_deliveries_subscription_id_created_at", table_name="webhook_deliveries"
    )
    op.drop_index("ix_webhook_deliveries_next_attempt_at", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_city_carrier_map_is_confirmed", table_name="city_carrier_map")
    op.drop_index("ix_city_carrier_map_carrier_id_carrier_city_code", table_name="city_carrier_map")
    op.drop_table("city_carrier_map")
    op.drop_index("ix_carrier_terminals_city_fias_id_carrier_id", table_name="carrier_terminals")
    op.drop_table("carrier_terminals")
    op.drop_table("carrier_services")
    op.drop_index("ix_carrier_score_snapshots_lookup", table_name="carrier_score_snapshots")
    op.drop_table("carrier_score_snapshots")
    op.drop_index("ix_carrier_accounts_tenant_id_is_active", table_name="carrier_accounts")
    op.drop_index(op.f("ix_carrier_accounts_tenant_id"), table_name="carrier_accounts")
    op.drop_table("carrier_accounts")
    op.drop_index("ix_audit_log_tenant_id_created_at", table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_tenant_id"), table_name="audit_log")
    op.drop_index("ix_audit_log_entity_type_entity_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_addresses_tenant_id_counterparty_id", table_name="addresses")
    op.drop_index(op.f("ix_addresses_tenant_id"), table_name="addresses")
    op.drop_index("ix_addresses_city_fias_id", table_name="addresses")
    op.drop_table("addresses")
    op.drop_index(
        "ix_webhook_subscriptions_tenant_id_is_active", table_name="webhook_subscriptions"
    )
    op.drop_index(op.f("ix_webhook_subscriptions_tenant_id"), table_name="webhook_subscriptions")
    op.drop_table("webhook_subscriptions")
    op.drop_index(op.f("ix_users_tenant_id"), table_name="users")
    op.drop_index("ix_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("tenants")
    op.drop_index("ix_rate_requests_tenant_id_hash", table_name="rate_requests")
    op.drop_index("ix_rate_requests_tenant_id_created_at", table_name="rate_requests")
    op.drop_index(op.f("ix_rate_requests_tenant_id"), table_name="rate_requests")
    op.drop_table("rate_requests")
    op.drop_index("ix_counterparties_tenant_id_name", table_name="counterparties")
    op.drop_index("ix_counterparties_tenant_id_inn", table_name="counterparties")
    op.drop_index(op.f("ix_counterparties_tenant_id"), table_name="counterparties")
    op.drop_table("counterparties")
    op.drop_index("ix_cities_name", table_name="cities")
    op.drop_index("ix_cities_kladr_id", table_name="cities")
    op.drop_table("cities")
    op.drop_table("carriers")
    op.drop_index("ix_carrier_raw_calls_tenant_id_created_at", table_name="carrier_raw_calls")
    op.drop_index(op.f("ix_carrier_raw_calls_tenant_id"), table_name="carrier_raw_calls")
    op.drop_index("ix_carrier_raw_calls_shipment_id", table_name="carrier_raw_calls")
    op.drop_index("ix_carrier_raw_calls_expires_at", table_name="carrier_raw_calls")
    op.drop_table("carrier_raw_calls")
    op.drop_index("ix_api_keys_tenant_id_revoked_at", table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_tenant_id"), table_name="api_keys")
    op.drop_table("api_keys")
