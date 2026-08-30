"""Контракт ``CarrierAdapter`` и DTO.

Единственный контракт между ядром и любым перевозчиком. **Меняется только через ADR**
(CLAUDE.md §7): его переделка означает переписывание всех адаптеров.

Правила:

* DTO неизменяемы (``frozen=True``). Мутация состояния между слоями — источник ошибок,
  которые агент вносит особенно охотно.
* Ни один тип из библиотеки конкретного ТК не пересекает границу модуля адаптера.
* ``extras`` — единственный канал для специфики перевозчика, и он типизируется
  на уровне адаптера, а не здесь.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from aerogram.shared.enums import CargoType, LabelFormat, PriceSource
from aerogram.shared.money import Money

__all__ = [
    "CancelResult",
    "Capabilities",
    "CarrierAccount",
    "CarrierAdapter",
    "CarrierCity",
    "CarrierServiceRow",
    "CarrierTerminalRow",
    "LabelResult",
    "Party",
    "Place",
    "Quote",
    "QuoteRequest",
    "RawEvent",
    "RefCatalog",
    "ShipmentRequest",
    "ShipmentResult",
]


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Что перевозчик реально умеет.

    Интерфейс не деградирует до наименьшего общего знаменателя: если ТК умеет больше,
    это доступно через ``extras`` (раздел 4.2 ТЗ).
    """

    supports_webhooks: bool = False
    supports_pickup_request: bool = False
    supports_cod: bool = False
    supports_insurance: bool = False
    supports_cancel: bool = False
    supports_terminals: bool = False
    #: Считает ли перевозчик объёмный вес сам. Если да — платформа его не досчитывает,
    #: иначе получится двойной учёт (FR-1.2).
    computes_volumetric_weight: bool = True
    max_places: int = 1
    supported_label_formats: tuple[LabelFormat, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Снимок для справочника ``carriers.capabilities`` и публичного API."""
        return {
            "supports_webhooks": self.supports_webhooks,
            "supports_pickup_request": self.supports_pickup_request,
            "supports_cod": self.supports_cod,
            "supports_insurance": self.supports_insurance,
            "supports_cancel": self.supports_cancel,
            "supports_terminals": self.supports_terminals,
            "computes_volumetric_weight": self.computes_volumetric_weight,
            "max_places": self.max_places,
            "supported_label_formats": [f.value for f in self.supported_label_formats],
        }


@dataclass(frozen=True, slots=True)
class CarrierAccount:
    """Учётные данные тенанта у перевозчика, уже расшифрованные.

    Адаптер получает готовые значения и **никогда** не обращается к БД и к шифрованию:
    расшифровка — забота вызывающего слоя.
    """

    account_id: str
    carrier_code: str
    mode: Literal["own_contract", "aerogram"]
    credentials: dict[str, str]
    is_sandbox: bool = True
    settings: dict[str, object] = field(default_factory=dict)

    @property
    def price_source(self) -> PriceSource:
        """Источник цены, соответствующий режиму договора (FR-1.5)."""
        return PriceSource.OWN_CONTRACT if self.mode == "own_contract" else PriceSource.AEROGRAM


@dataclass(frozen=True, slots=True)
class Place:
    """Одно грузовое место."""

    weight_kg: Decimal
    length_cm: int
    width_cm: int
    height_cm: int


@dataclass(frozen=True, slots=True)
class Party:
    """Отправитель или получатель в терминах адаптера."""

    city_fias_id: str | None
    city_name: str
    #: Код города или терминала в системе перевозчика, если известен.
    carrier_city_code: str | None = None
    terminal_code: str | None = None
    postal_code: str | None = None
    address: str | None = None
    name: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    inn: str | None = None


@dataclass(frozen=True, slots=True)
class QuoteRequest:
    """Нормализованный запрос расчёта."""

    sender: Party
    recipient: Party
    places: tuple[Place, ...]
    declared_value: Money
    cargo_type: CargoType
    pickup: bool
    delivery_to_door: bool
    insurance: bool = False
    cod_amount: Money | None = None
    required_delivery_date: date | None = None
    extras: dict[str, object] = field(default_factory=dict)

    @property
    def total_weight_kg(self) -> Decimal:
        return sum((p.weight_kg for p in self.places), start=Decimal("0"))


@dataclass(frozen=True, slots=True)
class Quote:
    """Одно предложение одного ТК по одному тарифу."""

    service_code: str
    tariff_code: str
    service_name: str
    price: Money
    transit_days_min: int
    transit_days_max: int
    promised_delivery_date: date | None
    price_source: PriceSource
    raw: dict[str, object] = field(default_factory=dict)
    #: Составляющие цены, если ТК их возвращает — для расшифровки строки выдачи.
    #: Ключ — тип составляющей, значение — сумма в той же валюте, что и ``price``.
    price_breakdown: dict[str, Money] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ShipmentRequest:
    """Запрос на создание заказа у перевозчика."""

    #: Внутренний номер Aerogram. Передаётся перевозчику как номер клиента —
    #: по нему выполняется сверка «призраков» (FR-2.5).
    number: str
    service_code: str
    tariff_code: str
    sender: Party
    recipient: Party
    places: tuple[Place, ...]
    declared_value: Money
    cargo_type: CargoType
    pickup: bool
    delivery_to_door: bool
    insurance: bool = False
    cod_amount: Money | None = None
    comment: str | None = None
    items: tuple[dict[str, object], ...] = ()
    extras: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ShipmentResult:
    """Результат создания заказа."""

    external_id: str
    tracking_number: str | None
    promised_delivery_date: date | None
    price_actual: Money | None
    #: true — перевозчик принял заказ асинхронно, трек-номер придёт позже.
    is_pending: bool = False
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CancelResult:
    """Результат отмены."""

    accepted: bool
    message: str | None = None
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LabelResult:
    """Печатная форма.

    ``content is None`` при ``is_pending`` — перевозчик формирует файл асинхронно.
    Асинхронность скрывается вызывающим слоем, но адаптер обязан её обозначить (FR-4.5).
    """

    format: LabelFormat
    content: bytes | None
    is_pending: bool = False
    external_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RawEvent:
    """Сырое событие трекинга до нормализации статуса."""

    occurred_at: datetime
    status_raw: str
    city: str | None = None
    comment: str | None = None
    raw: dict[str, object] = field(default_factory=dict)

    def dedup_key(self) -> str:
        """Отпечаток события: защита от дублей при polling поверх вебхуков."""
        return f"{self.occurred_at.isoformat()}|{self.status_raw}|{self.city or ''}"


@dataclass(frozen=True, slots=True)
class CarrierCity:
    """Город в справочнике перевозчика, с его собственным кодом.

    ``fias_id`` и ``kladr_id`` заполняются, если перевозчик их отдаёт: это
    детерминированные ключи сопоставления, и они избавляют от сравнения имён.
    """

    code: str
    name: str
    region: str | None = None
    fias_id: str | None = None
    kladr_id: str | None = None
    #: Сколько пунктов выдачи перевозчика в этом городе. Домен использует это
    #: как приоритет разбора очереди ручного сопоставления.
    terminals_count: int = 0


@dataclass(frozen=True, slots=True)
class CarrierTerminalRow:
    """Терминал или пункт выдачи в справочнике перевозчика."""

    external_code: str
    city_code: str | None = None
    city_fias_id: str | None = None
    city_name: str | None = None
    address: str | None = None
    type: Literal["pvz", "terminal", "postamat"] = "pvz"
    work_hours: str | None = None
    lat: float | None = None
    lon: float | None = None
    has_cash: bool = False
    has_card: bool = False
    max_weight_kg: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CarrierServiceRow:
    """Услуга или тариф в справочнике перевозчика."""

    code: str
    name: str
    mode: Literal["door_door", "door_terminal", "terminal_door", "terminal_terminal"]
    is_express: bool = True


@dataclass(frozen=True, slots=True)
class RefCatalog:
    """Справочники перевозчика, полученные адаптером.

    Адаптер ВОЗВРАЩАЕТ данные, а записывает их домен. Обратный порядок означал
    бы, что адаптер обращается к базе, а это запрещено (ADR-0005): адаптер
    нельзя было бы проверить на записанных фикстурах без БД, и он немедленно
    нарушил бы контракт ``carrier-isolation``.

    Пустой кортеж означает «перевозчик не отдаёт этот справочник», а не
    «справочник пуст»: домен на пустом наборе ничего не гасит.
    """

    cities: tuple[CarrierCity, ...] = ()
    terminals: tuple[CarrierTerminalRow, ...] = ()
    services: tuple[CarrierServiceRow, ...] = ()
    #: Признак полноты выгрузки. Если перевозчик отдал справочник частично
    #: (обрыв, пагинация не дочитана), домен обязан НЕ гасить отсутствующие
    #: записи: погасить всю сеть ПВЗ дороже, чем показать один закрытый.
    is_complete: bool = True


@runtime_checkable
class CarrierAdapter(Protocol):
    """Интерфейс перевозчика.

    Добавление нового ТК не должно требовать изменений в ядре, в кабинете и в
    публичном API — только новый модуль адаптера и запись в справочнике (раздел 4.2 ТЗ).

    Все методы обязаны укладываться в таймаут, заданный вызывающим слоем, и поднимать
    исключения из ``shared.errors`` — ошибка перевозчика никогда не даёт 500.
    """

    code: str
    name: str
    capabilities: Capabilities

    async def quote(self, req: QuoteRequest, acc: CarrierAccount) -> list[Quote]: ...

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult: ...

    async def label(self, ext_id: str, fmt: LabelFormat, acc: CarrierAccount) -> LabelResult: ...

    async def track(self, ext_id: str, acc: CarrierAccount) -> list[RawEvent]: ...

    async def cancel(self, ext_id: str, acc: CarrierAccount) -> CancelResult: ...

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        """Найти заказ по внутреннему номеру Aerogram.

        Используется сверкой «призраков» (FR-2.5): если ответ на создание не дошёл,
        платформа обязана выяснить, создан ли заказ на самом деле.
        """
        ...

    async def fetch_refs(self, acc: CarrierAccount) -> RefCatalog:
        """Выгрузить справочники перевозчика.

        Метод называется ``fetch``, а не ``sync``, намеренно: синхронизация —
        это запись в базу, а адаптер к базе не обращается. Он отдаёт данные,
        а сводит их с ФИАС и сохраняет ``directories`` (ADR-0009).
        """
        ...

    def parse_webhook(self, payload: dict[str, object]) -> list[RawEvent]: ...

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        """Проверить подпись или секрет вебхука (FR-3.1)."""
        ...
