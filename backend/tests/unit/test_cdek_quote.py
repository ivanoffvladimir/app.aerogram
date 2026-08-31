"""Расчёт СДЭК: нормализация тарифов, единицы измерения, фильтрация режимов.

Фикстуры синтетические — см. tests/fixtures/cdek/README.md. Структура сверена
с контрактом API 2.0, но не заменяет прогон на боевом контуре.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from aerogram.carriers.base import CarrierAccount, Party, Place, QuoteRequest
from aerogram.carriers.cdek.adapter import CdekAdapter
from aerogram.carriers.cdek.client import SANDBOX_BASE_URL, CdekClient
from aerogram.carriers.cdek.mapping import grams_from_kg, modes_for_request
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import CargoType, LabelFormat, PriceSource
from aerogram.shared.errors import CarrierError, CarrierValidationError
from aerogram.shared.money import Money
from aerogram.shared.schemas import PackageSchema

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cdek"


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> CdekAdapter:
    """Адаптер с подменённым транспортом: сеть не используется."""

    def factory(_: CarrierAccount) -> CdekClient:
        inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SANDBOX_BASE_URL)
        return CdekClient(client_id="i", client_secret="s", http_client=inner)

    return CdekAdapter(client_factory=factory)


def _handler_for(body: dict[str, object]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth" in str(request.url):
            return httpx.Response(200, json=load("oauth_ok"))
        return httpx.Response(200, json=body)

    return handler


@pytest.fixture
def account() -> CarrierAccount:
    return CarrierAccount(
        account_id="1",
        carrier_code="cdek",
        mode="own_contract",
        credentials={"client_id": "i", "client_secret": "s"},
    )


def _request(*, pickup: bool = True, to_door: bool = True, weight: str = "12.5") -> QuoteRequest:
    return QuoteRequest(
        sender=Party(
            city_fias_id="7b6de6a5-86d0-4735-b11a-499081111af8",
            city_name="Владивосток",
            carrier_city_code="75",
        ),
        recipient=Party(
            city_fias_id="0c5b2444-70a0-4932-980c-b4dc0d3f02b5",
            city_name="Москва",
            carrier_city_code="44",
        ),
        places=(Place(weight_kg=Decimal(weight), length_cm=40, width_cm=30, height_cm=25),),
        declared_value=Money.from_major("480000.00", "RUB"),
        cargo_type=CargoType.EQUIPMENT,
        pickup=pickup,
        delivery_to_door=to_door,
    )


class TestWeightUnits:
    """Калькулятор СДЭК принимает вес в граммах, а домен работает в килограммах."""

    @pytest.mark.parametrize(
        ("kg", "grams"),
        [("0.5", 500), ("1", 1000), ("12.5", 12500), ("0.001", 1)],
    )
    def test_converts_kilograms_to_grams(self, kg: str, grams: int) -> None:
        assert grams_from_kg(Decimal(kg)) == grams

    @pytest.mark.parametrize("grams", [1, 999, 10_100, 10_150, 12_345])
    def test_the_round_trip_from_the_api_loses_nothing(self, grams: int) -> None:
        """Граммы → килограммы → граммы обязаны сойтись ровно.

        Вес приходит целыми граммами, домен работает в килограммах,
        а перевозчику уходят снова граммы. Десятые доли килограмма —
        обычное дело (10,1 кг), и потеря на этом круге дала бы цену
        не за тот вес.
        """
        assert grams_from_kg(PackageSchema(weight_grams=grams).weight_kg) == grams

    def test_rounds_up_partial_grams(self) -> None:
        # Занизить вес значит недобрать с клиента: перевозчик посчитает
        # по фактическому и выставит счёт больше нашей котировки.
        assert grams_from_kg(Decimal("1.0001")) == 1001

    def test_rejects_non_positive_weight(self) -> None:
        with pytest.raises(ValueError, match="вес"):
            grams_from_kg(Decimal("0"))

    async def test_payload_carries_grams_not_kilograms(self, account: CarrierAccount) -> None:
        """Ошибка в единице измерения не даёт исключения — она даёт цену
        в тысячу раз другую, и обнаруживается счётом от перевозчика."""
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth" in str(request.url):
                return httpx.Response(200, json=load("oauth_ok"))
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=load("tarifflist_ok"))

        await _adapter(handler).quote(_request(weight="12.5"), account)

        packages = captured["packages"]
        assert isinstance(packages, list)
        assert packages[0]["weight"] == 12500


class TestDeliveryModeFiltering:
    """Режим доставки задаётся парой опций формы расчёта (FR-1.1)."""

    @pytest.mark.parametrize(
        ("pickup", "to_door", "expected"),
        [
            (True, True, {1}),
            (True, False, {2, 6}),
            (False, True, {3}),
            (False, False, {4, 7}),
        ],
    )
    def test_modes_match_the_requested_pair(
        self, pickup: bool, to_door: bool, expected: set[int]
    ) -> None:
        assert modes_for_request(pickup=pickup, delivery_to_door=to_door) == expected

    async def test_door_to_door_request_returns_only_door_to_door_tariffs(
        self, account: CarrierAccount
    ) -> None:
        """Цена до пункта выдачи всегда ниже, чем до двери.

        Попав в выдачу по запросу «до двери», она выиграла бы ранжирование
        по цене и увела бы пользователя к заведомо неверному варианту.
        """
        quotes = await _adapter(_handler_for(load("tarifflist_ok"))).quote(
            _request(pickup=True, to_door=True), account
        )

        assert [q.tariff_code for q in quotes] == ["136"]
        assert quotes[0].raw["delivery_mode"] == 1

    async def test_terminal_to_terminal_request_includes_postamat(
        self, account: CarrierAccount
    ) -> None:
        # Для отправителя постамат и склад — один сценарий: получатель забирает сам.
        quotes = await _adapter(_handler_for(load("tarifflist_ok"))).quote(
            _request(pickup=False, to_door=False), account
        )
        assert sorted(q.tariff_code for q in quotes) == ["137", "366"]


class TestQuoteMapping:
    async def test_maps_tariff_fields(self, account: CarrierAccount) -> None:
        quotes = await _adapter(_handler_for(load("tarifflist_ok"))).quote(
            _request(pickup=True, to_door=True), account
        )

        quote = quotes[0]
        assert quote.service_name == "Посылка дверь-дверь"
        assert quote.price.currency == "RUB"
        assert quote.transit_days_min == 2
        assert quote.transit_days_max == 3

    async def test_price_is_exact_minor_units_without_float_error(
        self, account: CarrierAccount
    ) -> None:
        """Деньги — целое число минорных единиц (ADR-0011).

        Путь через float закрепил бы потерю точности: 2450.5 рубля в float
        не представимо точно, и умножение на 100 дало бы 245049 копеек.
        Проверяем именно точное значение, а не приблизительное равенство.
        """
        body = {
            "tariff_codes": [
                {
                    "tariff_code": 137,
                    "tariff_name": "Посылка склад-склад",
                    "delivery_mode": 4,
                    "delivery_sum": 2450.5,
                    "period_min": 2,
                    "period_max": 3,
                }
            ]
        }
        quotes = await _adapter(_handler_for(body)).quote(
            _request(pickup=False, to_door=False), account
        )

        assert quotes[0].price == Money(245_050, "RUB")
        assert isinstance(quotes[0].price.amount_minor, int)

    async def test_promised_date_uses_calendar_days(self, account: CarrierAccount) -> None:
        """Плановая дата — обещание в календаре, а не в рабочих днях.

        Сравнение с фактом по календарю и пересчёт в рабочие дни — забота
        домена (FR-6.2), но обещанная клиенту дата обязана быть календарной.
        """
        quotes = await _adapter(_handler_for(load("tarifflist_ok"))).quote(
            _request(pickup=True, to_door=True), account
        )
        assert quotes[0].promised_delivery_date is not None
        delta = quotes[0].promised_delivery_date - utcnow().date()
        assert delta.days == 4  # calendar_max из фикстуры

    async def test_price_source_follows_account_mode(self, account: CarrierAccount) -> None:
        quotes = await _adapter(_handler_for(load("tarifflist_ok"))).quote(
            _request(pickup=True, to_door=True), account
        )
        assert quotes[0].price_source is PriceSource.OWN_CONTRACT

    async def test_row_without_price_is_dropped(self, account: CarrierAccount) -> None:
        """Строка без цены не должна становиться нулевой котировкой.

        Ноль выиграл бы сортировку по цене и встал первым в выдаче.
        """
        body = {"tariff_codes": [{"tariff_code": 136, "delivery_mode": 1, "tariff_name": "X"}]}
        quotes = await _adapter(_handler_for(body)).quote(
            _request(pickup=True, to_door=True), account
        )
        assert quotes == []

    async def test_empty_tariff_list_is_not_an_error(self, account: CarrierAccount) -> None:
        # Направление может не обслуживаться — это пустая выдача, а не сбой.
        quotes = await _adapter(_handler_for({"tariff_codes": []})).quote(_request(), account)
        assert quotes == []


class TestLocationResolution:
    async def test_prefers_carrier_city_code(self, account: CarrierAccount) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth" in str(request.url):
                return httpx.Response(200, json=load("oauth_ok"))
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=load("tarifflist_ok"))

        await _adapter(handler).quote(_request(), account)

        assert captured["from_location"] == {"code": 75}
        assert captured["to_location"] == {"code": 44}

    async def test_falls_back_to_postal_code(self, account: CarrierAccount) -> None:
        """Без сопоставления города индекс точнее названия.

        Одноимённых населённых пунктов в России десятки, и название без
        уточнения отправляет груз не туда.
        """
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth" in str(request.url):
                return httpx.Response(200, json=load("oauth_ok"))
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=load("tarifflist_ok"))

        request = QuoteRequest(
            sender=Party(city_fias_id=None, city_name="Владивосток", postal_code="690000"),
            recipient=Party(city_fias_id=None, city_name="Москва", postal_code="101000"),
            places=(Place(Decimal("1"), 10, 10, 10),),
            declared_value=Money.from_major("1000", "RUB"),
            cargo_type=CargoType.PARCEL,
            pickup=True,
            delivery_to_door=True,
        )
        await _adapter(handler).quote(request, account)

        assert captured["from_location"] == {"postal_code": "690000"}

    async def test_falls_back_to_city_name_last(self, account: CarrierAccount) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth" in str(request.url):
                return httpx.Response(200, json=load("oauth_ok"))
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=load("tarifflist_ok"))

        request = QuoteRequest(
            sender=Party(city_fias_id=None, city_name="Владивосток"),
            recipient=Party(city_fias_id=None, city_name="Москва"),
            places=(Place(Decimal("1"), 10, 10, 10),),
            declared_value=Money.from_major("1000", "RUB"),
            cargo_type=CargoType.PARCEL,
            pickup=True,
            delivery_to_door=True,
        )
        await _adapter(handler).quote(request, account)

        assert captured["from_location"] == {"country_code": "RU", "city": "Владивосток"}


class TestErrors:
    async def test_carrier_error_becomes_domain_error_with_russian_text(
        self, account: CarrierAccount
    ) -> None:
        """Ошибка перевозчика не даёт 500 (раздел 8.2 ТЗ).

        Вызывающий слой превращает её в отдельную строку выдачи, и расчёт
        по остальным перевозчикам продолжается (FR-1.4).
        """
        with pytest.raises(CarrierValidationError, match="Город отправителя не найден") as info:
            await _adapter(_handler_for(load("tarifflist_error"))).quote(_request(), account)

        assert info.value.http_status == 502
        assert info.value.carrier_code == "cdek"


class TestUnimplementedMethods:
    """Методы недель 6-8 отказывают явно, а не возвращают пустоту.

    Пустой список или None здесь выглядели бы как «перевозчик ничего не вернул»
    и разошлись бы по домену как данные: отправление без трек-номера,
    отчёт без событий.
    """

    async def test_create_is_declared_but_refuses(self, account: CarrierAccount) -> None:
        with pytest.raises(CarrierError, match="создание отправления"):
            await CdekAdapter().create(None, account)  # type: ignore[arg-type]

    async def test_cancel_is_declared_but_refuses(self, account: CarrierAccount) -> None:
        with pytest.raises(CarrierError, match="отмена"):
            await CdekAdapter().cancel("ext-1", account)

    async def test_ghost_reconciliation_is_declared_but_refuses(
        self, account: CarrierAccount
    ) -> None:
        with pytest.raises(CarrierError, match="призрак"):
            await CdekAdapter().find_by_number("AG-1", account)

    async def test_label_is_declared_but_refuses(self, account: CarrierAccount) -> None:
        with pytest.raises(CarrierError, match="печатная форма"):
            await CdekAdapter().label("ext-1", LabelFormat.PDF_A6, account)

    async def test_track_is_declared_but_refuses(self, account: CarrierAccount) -> None:
        with pytest.raises(CarrierError, match="трекинг"):
            await CdekAdapter().track("ext-1", account)

    def test_webhook_methods_are_declared(self) -> None:
        adapter = CdekAdapter()
        with pytest.raises(CarrierError, match="вебхук"):
            adapter.parse_webhook({})
        with pytest.raises(CarrierError, match="подпис"):
            adapter.verify_webhook(b"", {}, "secret")

    async def test_refusal_is_a_carrier_error_not_a_crash(self, account: CarrierAccount) -> None:
        # 502, а не 500: отвечает плохо внешняя система, а не наша.
        with pytest.raises(CarrierError) as info:
            await CdekAdapter().track("ext-1", account)
        assert info.value.http_status == 502


class TestCapabilities:
    def test_cdek_computes_volumetric_weight_itself(self) -> None:
        """Досчитывать объёмный вес на нашей стороне значило бы учесть его дважды."""
        assert CdekAdapter.capabilities.computes_volumetric_weight is True

    def test_declares_label_formats(self) -> None:
        formats = [f.value for f in CdekAdapter.capabilities.supported_label_formats]
        assert "pdf_a6" in formats
