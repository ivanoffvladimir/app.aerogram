"""Платформенные справочники.

Общие для всех тенантов, поэтому без ``tenant_id`` и без RLS: перевозчики, их услуги,
терминалы и ПВЗ, города ФИАС и кросс-таблица кодов городов у перевозчиков.

``city_carrier_map`` — критичная таблица: сопоставление городов есть источник
большинства ошибок в мультиперевозочных системах (раздел 5.1 ТЗ).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aerogram.db import Base, TimestampMixin, uuid_pk

__all__ = [
    "Carrier",
    "CarrierService",
    "CarrierTerminal",
    "City",
    "CityCarrierMap",
    "CityMappingQueue",
]


class Carrier(Base, TimestampMixin):
    """Перевозчик. Добавление нового ТК = новый адаптер + строка здесь (раздел 4.2 ТЗ)."""

    __tablename__ = "carriers"

    id: Mapped[UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    logo_url: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Снимок Capabilities адаптера — чтобы фронт и API не импортировали код адаптера.
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    #: Делитель объёмного веса перевозчика (FR-1.2), по умолчанию 5000.
    volumetric_divisor: Mapped[int] = mapped_column(Integer, nullable=False, default=5000)

    __table_args__ = (UniqueConstraint("code", name="uq_carriers_code"),)


class CarrierService(Base, TimestampMixin):
    """Услуга/тариф перевозчика."""

    __tablename__ = "carrier_services"

    id: Mapped[UUID] = uuid_pk()
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    is_express: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("carrier_id", "code", name="uq_carrier_services_carrier_id_code"),
        CheckConstraint(
            "mode IN ('door_door', 'door_terminal', 'terminal_door', 'terminal_terminal')",
            name="carrier_service_mode",
        ),
    )


class CarrierTerminal(Base, TimestampMixin):
    """Терминал или ПВЗ перевозчика. Синхронизируется ежесуточно (FR-8.3)."""

    __tablename__ = "carrier_terminals"

    id: Mapped[UUID] = uuid_pk()
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False
    )
    external_code: Mapped[str] = mapped_column(String(50), nullable=False)
    city_fias_id: Mapped[str | None] = mapped_column(String(36))
    city_name: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(20), nullable=False, default="pvz")
    work_hours: Mapped[str | None] = mapped_column(String(255))
    lat: Mapped[float | None] = mapped_column()
    lon: Mapped[float | None] = mapped_column()
    has_cash: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_card: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_weight_kg: Mapped[float | None] = mapped_column(Numeric(10, 3))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Момент, когда терминал перестал приходить в выгрузке перевозчика.
    #: Строка не удаляется: её код лежит в уже созданных отправлениях,
    #: и карточка старого заказа обязана остаться читаемой.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "carrier_id", "external_code", name="uq_carrier_terminals_carrier_id_external_code"
        ),
        CheckConstraint("type IN ('pvz', 'terminal', 'postamat')", name="carrier_terminal_type"),
        Index("ix_carrier_terminals_city_fias_id_carrier_id", "city_fias_id", "carrier_id"),
    )


class City(Base, TimestampMixin):
    """Город из ФИАС — единый ключ адресации между перевозчиками (FR-8.1)."""

    __tablename__ = "cities"

    id: Mapped[UUID] = uuid_pk()
    fias_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Читаемое наименование, собранное ТОЛЬКО из полей городского уровня.
    #: Улица и дом сюда попасть не могут: таблица общая для всех тенантов
    #: и под RLS не находится (12.1, 12.7 ТЗ).
    full_name: Mapped[str | None] = mapped_column(String(500))
    #: Уровень объекта ФИАС. Без него город (4) и посёлок (6) в таблице
    #: неразличимы, а Москва (1) выглядит аномалией.
    fias_level: Mapped[int | None] = mapped_column(SmallInteger)
    #: Следующий элемент лестницы ключа: для Алупки — Ялта, для Зеленограда —
    #: Москва. Нужен управляемому откату сопоставления с перевозчиком.
    parent_fias_id: Mapped[str | None] = mapped_column(String(36))
    region: Mapped[str | None] = mapped_column(String(255))
    region_fias_id: Mapped[str | None] = mapped_column(String(36))
    kladr_id: Mapped[str | None] = mapped_column(String(19))
    postal_code: Mapped[str | None] = mapped_column(String(10))
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, default="RU")
    timezone: Mapped[str | None] = mapped_column(String(64))
    lat: Mapped[float | None] = mapped_column()
    lon: Mapped[float | None] = mapped_column()
    population: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("fias_id", name="uq_cities_fias_id"),
        Index("ix_cities_name", "name"),
        Index("ix_cities_kladr_id", "kladr_id"),
        Index("ix_cities_parent_fias_id", "parent_fias_id"),
    )


class CityCarrierMap(Base, TimestampMixin):
    """Сопоставление города ФИАС коду города у перевозчика (FR-8.2).

    Несопоставленные города попадают в очередь ручного сопоставления в админке:
    ``is_confirmed = false`` и заполненный ``carrier_city_name`` без ``city_fias_id``
    в исходных данных синхронизации.
    """

    __tablename__ = "city_carrier_map"

    id: Mapped[UUID] = uuid_pk()
    city_fias_id: Mapped[str] = mapped_column(String(36), nullable=False)
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False
    )
    carrier_city_code: Mapped[str] = mapped_column(String(50), nullable=False)
    carrier_city_name: Mapped[str | None] = mapped_column(String(255))
    #: false — сопоставлено автоматически и ждёт подтверждения человеком.
    #: Флаг управляет вниманием администратора, а не использованием строки:
    #: если бы неподтверждённые записи не использовались, платформа не работала
    #: бы до ручного разбора тысяч городов.
    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Чем сопоставлено: fias, kladr, exact_name, fuzzy_name, manual.
    #: Хранится, чтобы решение можно было объяснить постфактум.
    match_method: Mapped[str | None] = mapped_column(String(20))
    match_score: Mapped[float | None] = mapped_column(Numeric(4, 3))
    confirmed_by_user_id: Mapped[UUID | None] = mapped_column()
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "carrier_id", "city_fias_id", name="uq_city_carrier_map_carrier_id_city_fias_id"
        ),
        Index(
            "ix_city_carrier_map_carrier_id_carrier_city_code",
            "carrier_id",
            "carrier_city_code",
        ),
        Index("ix_city_carrier_map_is_confirmed", "is_confirmed"),
    )


class CityMappingQueue(Base, TimestampMixin):
    """Очередь ручного сопоставления городов (FR-8.2, FR-12.3).

    Отдельная таблица, а не строки ``city_carrier_map`` с пустым городом:
    в ``city_carrier_map`` колонка ``city_fias_id`` объявлена NOT NULL, и
    хранить там гипотезы физически нельзя. Кроме того, у записи очереди своя
    жизнь — кандидаты, оценка, решение человека, — которой нет у сопоставления.

    Платформенная таблица: города общие для всех тенантов, RLS не применяется.
    """

    __tablename__ = "city_mapping_queue"

    id: Mapped[UUID] = uuid_pk()
    carrier_id: Mapped[UUID] = mapped_column(
        ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False
    )
    carrier_city_code: Mapped[str] = mapped_column(String(50), nullable=False)
    carrier_city_name: Mapped[str | None] = mapped_column(String(255))
    carrier_region_name: Mapped[str | None] = mapped_column(String(255))
    #: Почему попало в очередь: no_match, ambiguous, conflict.
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Кандидаты с оценками — без них администратору нечего нажать.
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    best_score: Mapped[float | None] = mapped_column(Numeric(4, 3))
    #: Сколько терминалов перевозчика висит на этом городе: приоритет разбора.
    terminals_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[UUID | None] = mapped_column()
    resolved_city_fias_id: Mapped[str | None] = mapped_column(String(36))

    __table_args__ = (
        UniqueConstraint(
            "carrier_id", "carrier_city_code", name="uq_city_mapping_queue_carrier_id_code"
        ),
        CheckConstraint(
            "reason IN ('no_match', 'ambiguous', 'conflict')", name="city_mapping_queue_reason"
        ),
        Index("ix_city_mapping_queue_open", "carrier_id", "terminals_count"),
    )
