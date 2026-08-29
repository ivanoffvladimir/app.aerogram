"""Модели ядра.

Здесь же ``carrier_accounts`` (учётные данные тенанта у перевозчика) и
``carrier_raw_calls`` (сырьё вызовов ТК): обе таблицы принадлежат тенанту, а не
платформенному справочнику, и обе — предмет требований по ПДн и секретам.

ВНИМАНИЕ (CLAUDE.md §7): схема БД и RLS меняются только с построчным ревью человека.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aerogram.db import Base, TenantMixin, TimestampMixin, uuid_pk
from aerogram.shared.enums import CarrierAccountMode, TenantStatus, UserRole

__all__ = [
    "Address",
    "ApiKey",
    "AuditLog",
    "CarrierAccount",
    "CarrierRawCall",
    "Counterparty",
    "Tenant",
    "User",
]


class Tenant(Base, TimestampMixin):
    """Компания-клиент платформы. Платформенная таблица, RLS не применяется."""

    __tablename__ = "tenants"

    id: Mapped[UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    inn: Mapped[str | None] = mapped_column(String(12))
    kpp: Mapped[str | None] = mapped_column(String(9))
    legal_address: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TenantStatus] = mapped_column(
        String(20), nullable=False, default=TenantStatus.TRIAL
    )
    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="pilot")
    #: Таймзона тенанта: хранение всегда в UTC, отображение — по ней.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Moscow")
    #: Веса комбинированного ранга (FR-5.3), настраиваются на уровне тенанта.
    ranking_weights: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default='{"price": 0.4, "transit": 0.3, "score": 0.3}'
    )

    __table_args__ = (UniqueConstraint("inn", name="uq_tenants_inn"),)


class User(Base, TenantMixin, TimestampMixin):
    """Пользователь тенанта. Платформенные роли живут в том же справочнике ролей."""

    __tablename__ = "users"

    id: Mapped[UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(String(30), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Секрет TOTP. Для роли owner двухфакторная аутентификация обязательна (12.5 ТЗ).
    mfa_secret: Mapped[str | None] = mapped_column(String(255))
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        # Один и тот же человек может быть пользователем нескольких тенантов,
        # поэтому уникальность — по паре, а не по одному email.
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
        Index("ix_users_email_lower", "email"),
    )


class ApiKey(Base, TenantMixin, TimestampMixin):
    """Машинный доступ по ключу (FR-10.2). В БД хранится только хеш."""

    __tablename__ = "api_keys"

    id: Mapped[UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Префикс для отображения в кабинете: "ak_live_a1b2…", сам ключ не восстановим.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
        Index("ix_api_keys_tenant_id_revoked_at", "tenant_id", "revoked_at"),
    )


class AuditLog(Base, TenantMixin):
    """Аудит изменяющих операций и обращений поддержки к данным тенанта (12.6 ТЗ)."""

    __tablename__ = "audit_log"

    id: Mapped[UUID] = uuid_pk()
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    #: Признак имперсонации: обращение поддержки видно пользователю тенанта (раздел 2.2 ТЗ).
    impersonated_by_user_id: Mapped[UUID | None] = mapped_column()
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column()
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    payload_diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_audit_log_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_audit_log_entity_type_entity_id", "entity_type", "entity_id"),
    )


class Counterparty(Base, TenantMixin, TimestampMixin):
    """Контрагент тенанта: отправитель или получатель (адресная книга, FR-8.4)."""

    __tablename__ = "counterparties"

    id: Mapped[UUID] = uuid_pk()
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # legal/individual/entrepreneur
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    inn: Mapped[str | None] = mapped_column(String(12))
    kpp: Mapped[str | None] = mapped_column(String(9))
    contact_person: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    addresses: Mapped[list[Address]] = relationship(
        back_populates="counterparty", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('legal', 'individual', 'entrepreneur')", name="counterparty_type"
        ),
        Index("ix_counterparties_tenant_id_inn", "tenant_id", "inn"),
        Index("ix_counterparties_tenant_id_name", "tenant_id", "name"),
    )


class Address(Base, TenantMixin, TimestampMixin):
    """Адрес контрагента, нормализованный по ФИАС (FR-8.1)."""

    __tablename__ = "addresses"

    id: Mapped[UUID] = uuid_pk()
    counterparty_id: Mapped[UUID] = mapped_column(
        ForeignKey("counterparties.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(255))
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, default="RU")
    region: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Единый ключ сопоставления городов между перевозчиками (FR-8.1).
    city_fias_id: Mapped[str | None] = mapped_column(String(36))
    postal_code: Mapped[str | None] = mapped_column(String(10))
    street: Mapped[str | None] = mapped_column(String(255))
    house: Mapped[str | None] = mapped_column(String(50))
    flat: Mapped[str | None] = mapped_column(String(50))
    lat: Mapped[float | None] = mapped_column()
    lon: Mapped[float | None] = mapped_column()
    is_default_sender: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Следующий элемент лестницы ключа города — для отката сопоставления,
    #: когда своего кода у населённого пункта у перевозчика нет.
    city_parent_fias_id: Mapped[str | None] = mapped_column(String(36))
    #: Для чего адрес годится: door / locality / unusable. Единой проверки
    #: «адрес валиден» недостаточно: до пункта выдачи дом не нужен, до двери
    #: обязателен.
    fitness: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unusable", server_default="unusable"
    )
    #: Момент успешной нормализации. NULL означает «адрес введён руками
    #: и через справочник не проходил» — такой адрес не блокируется, но помечен.
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    counterparty: Mapped[Counterparty] = relationship(back_populates="addresses")

    __table_args__ = (
        Index("ix_addresses_tenant_id_counterparty_id", "tenant_id", "counterparty_id"),
        Index("ix_addresses_city_fias_id", "city_fias_id"),
    )


class CarrierAccount(Base, TenantMixin, TimestampMixin):
    """Пара «тенант × перевозчик»: реквизиты доступа и режим договора.

    ``credentials_encrypted`` — конверт AES-GCM (``shared.crypto``). Открытых
    учётных данных в БД нет ни в каком виде (12.3 ТЗ).
    """

    __tablename__ = "carrier_accounts"

    id: Mapped[UUID] = uuid_pk()
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="RESTRICT"), nullable=False
    )
    mode: Mapped[CarrierAccountMode] = mapped_column(String(20), nullable=False)
    credentials_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    contract_number: Mapped[str | None] = mapped_column(String(100))
    is_sandbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="unchecked")
    status_message: Mapped[str | None] = mapped_column(Text)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Специфика подключения, не являющаяся секретом: номера договоров, филиал оплаты и т. п.
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "carrier_id", "mode", name="uq_carrier_accounts_tenant_id_carrier_id_mode"
        ),
        CheckConstraint("mode IN ('own_contract', 'aerogram')", name="carrier_account_mode"),
        Index("ix_carrier_accounts_tenant_id_is_active", "tenant_id", "is_active"),
    )


class CarrierRawCall(Base, TenantMixin):
    """Сырьё вызовов перевозчика: запрос и ответ, с маскированием ПДн и секретов.

    Хранится 30 суток (раздел 8.2 ТЗ, п. 6) — это исходные данные для разбора
    спорных ситуаций. Чистится задачей ``worker.tasks.purge_raw_calls``.
    """

    __tablename__ = "carrier_raw_calls"

    id: Mapped[UUID] = uuid_pk()
    carrier_code: Mapped[str] = mapped_column(String(30), nullable=False)
    operation: Mapped[str] = mapped_column(String(30), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    shipment_id: Mapped[UUID | None] = mapped_column()
    http_status: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    request_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    #: Момент, после которого запись подлежит удалению.
    expires_at: Mapped[date] = mapped_column(Date, nullable=False)

    __table_args__ = (
        Index("ix_carrier_raw_calls_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_carrier_raw_calls_expires_at", "expires_at"),
        Index("ix_carrier_raw_calls_shipment_id", "shipment_id"),
    )
