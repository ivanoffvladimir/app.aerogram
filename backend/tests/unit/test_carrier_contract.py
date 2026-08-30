"""Реализуемость контракта ``CarrierAdapter``.

Смысл файла: поддельный перевозчик реализует протокол ЦЕЛИКОМ, без доступа
к базе и к сети. Если очередной метод контракта окажется неисполнимым в этих
условиях — как оказался прежний ``sync_refs``, возвращавший счётчики записи
в базу, — это выяснится здесь, а не на пятой неделе при первом настоящем
адаптере, когда цена правки контракта уже другая.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from aerogram.carriers.base import (
    CancelResult,
    Capabilities,
    CarrierAccount,
    CarrierAdapter,
    CarrierCity,
    CarrierServiceRow,
    CarrierTerminalRow,
    LabelResult,
    Party,
    Quote,
    QuoteRequest,
    RawEvent,
    RefCatalog,
    ShipmentRequest,
    ShipmentResult,
)
from aerogram.shared.enums import CargoType, LabelFormat, PriceSource


class FakeCarrier:
    """Перевозчик для тестов. Реализует контракт целиком и ни к чему не ходит."""

    code = "fake"
    name = "Поддельный перевозчик"
    capabilities = Capabilities(
        supports_webhooks=True,
        supports_cancel=True,
        supports_terminals=True,
        max_places=10,
        supported_label_formats=(LabelFormat.PDF_A6,),
    )

    async def quote(self, req: QuoteRequest, acc: CarrierAccount) -> list[Quote]:
        return [
            Quote(
                service_code="EXPRESS",
                tariff_code="1",
                service_name="Экспресс",
                price=Decimal("2450.00"),
                currency="RUB",
                transit_days_min=2,
                transit_days_max=3,
                promised_delivery_date=None,
                price_source=acc.price_source,
            )
        ]

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult:
        return ShipmentResult(
            external_id=f"ext-{req.number}",
            tracking_number=f"TRK{req.number}",
            promised_delivery_date=None,
            price_actual=Decimal("2450.00"),
        )

    async def label(self, ext_id: str, fmt: LabelFormat, acc: CarrierAccount) -> LabelResult:
        return LabelResult(format=fmt, content=b"%PDF-1.4", is_pending=False)

    async def track(self, ext_id: str, acc: CarrierAccount) -> list[RawEvent]:
        return [RawEvent(occurred_at=datetime(2026, 8, 29, tzinfo=UTC), status_raw="DELIVERED")]

    async def cancel(self, ext_id: str, acc: CarrierAccount) -> CancelResult:
        return CancelResult(accepted=True)

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        return None

    async def fetch_refs(self, acc: CarrierAccount) -> RefCatalog:
        """Справочники ОТДАЮТСЯ, а не записываются.

        Это и есть проверяемое свойство: реализация не нуждается ни в сессии
        БД, ни в репозитории — только в учётных данных.
        """
        return RefCatalog(
            cities=(CarrierCity(code="MSK", name="Москва", region="Москва"),),
            terminals=(CarrierTerminalRow(external_code="MSK-1", city_code="MSK"),),
            services=(CarrierServiceRow(code="EXPRESS", name="Экспресс", mode="door_door"),),
        )

    def parse_webhook(self, payload: dict[str, object]) -> list[RawEvent]:
        return [RawEvent(occurred_at=datetime(2026, 8, 29, tzinfo=UTC), status_raw="IN_TRANSIT")]

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        expected = hmac.new(secret.encode(), payload, "sha256").hexdigest()
        return hmac.compare_digest(expected, headers.get("x-signature", ""))


@pytest.fixture
def adapter() -> FakeCarrier:
    return FakeCarrier()


@pytest.fixture
def account() -> CarrierAccount:
    return CarrierAccount(
        account_id="1",
        carrier_code="fake",
        mode="own_contract",
        credentials={"client_id": "x", "client_secret": "y"},
    )


class TestContractIsImplementable:
    def test_fake_adapter_satisfies_the_protocol(self, adapter: FakeCarrier) -> None:
        assert isinstance(adapter, CarrierAdapter)

    async def test_every_method_of_the_protocol_exists(self, adapter: FakeCarrier) -> None:
        # Protocol проверяет только наличие имён, поэтому реализуемость
        # подтверждается фактическим вызовом каждого метода.
        for name in (
            "quote",
            "create",
            "label",
            "track",
            "cancel",
            "find_by_number",
            "fetch_refs",
            "parse_webhook",
            "verify_webhook",
        ):
            assert callable(getattr(adapter, name)), f"метод {name} отсутствует"


class TestFetchRefs:
    async def test_returns_data_not_counters(
        self, adapter: FakeCarrier, account: CarrierAccount
    ) -> None:
        """Ключевое свойство после ADR-0009.

        Прежняя подпись ``sync_refs(acc) -> RefSyncReport`` возвращала счётчики
        записанных строк — то есть требовала, чтобы адаптер сам писал в базу.
        Адаптеру доступ к БД запрещён (ADR-0005), поэтому исполнить её было
        невозможно ни одному перевозчику.
        """
        catalog = await adapter.fetch_refs(account)

        assert catalog.cities[0].code == "MSK"
        assert catalog.terminals[0].external_code == "MSK-1"
        assert catalog.services[0].mode == "door_door"

    async def test_defaults_to_complete(
        self, adapter: FakeCarrier, account: CarrierAccount
    ) -> None:
        assert (await adapter.fetch_refs(account)).is_complete is True

    def test_empty_catalog_means_carrier_has_no_such_reference(self) -> None:
        """Пустой кортеж — «перевозчик не отдаёт справочник», а не «он пуст».

        Различие принципиально для гашения: на пустом наборе домен не имеет
        права погасить всю сеть пунктов выдачи.
        """
        catalog = RefCatalog()
        assert catalog.cities == ()
        assert catalog.terminals == ()
        assert catalog.is_complete is True

    def test_partial_catalog_is_marked(self) -> None:
        assert RefCatalog(cities=(), is_complete=False).is_complete is False


class TestAdapterNeedsNoDatabase:
    def test_module_does_not_import_persistence(self) -> None:
        """Адаптеры не знают о базе (ADR-0005).

        Проверка на уровне исходника: контракт не должен даже упоминать
        сессию или модели, иначе следующий адаптер напишут «как в примере».
        """
        import inspect

        from aerogram.carriers import base

        source = inspect.getsource(base)
        for forbidden in ("AsyncSession", "sqlalchemy", "aerogram.db", "Repository"):
            assert forbidden not in source, f"контракт адаптера упоминает {forbidden}"


class TestQuoteRespectsAccountMode:
    async def test_own_contract_is_reported_as_price_source(
        self, adapter: FakeCarrier, account: CarrierAccount
    ) -> None:
        quotes = await adapter.quote(
            QuoteRequest(
                sender=Party(city_fias_id=None, city_name="Москва"),
                recipient=Party(city_fias_id=None, city_name="Новосибирск"),
                places=(),
                declared_value=Decimal("1000"),
                cargo_type=CargoType.PARCEL,
                pickup=True,
                delivery_to_door=True,
            ),
            account,
        )
        assert quotes[0].price_source is PriceSource.OWN_CONTRACT
