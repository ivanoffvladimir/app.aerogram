"""Расчёт Почты России: единицы, деньги, сроки, индексы и продукты.

Фикстуры синтетические — у Почты нет ни машинной спецификации на расчёт,
ни примеров ответа в справке, только структура полей. Что именно это
доказывает, а что нет, написано в `tests/fixtures/pochta/README.md`.

Сеть не используется: транспорт подменяется целиком. Боевого адреса API
у Почты в документации нет вовсе, поэтому ходить всё равно некуда.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from aerogram.carriers import registry
from aerogram.carriers.base import (
    CarrierAccount,
    CarrierAdapter,
    Party,
    Place,
    QuoteRequest,
)
from aerogram.carriers.pochta.adapter import POCHTA_CODE, PochtaAdapter
from aerogram.carriers.pochta.client import SANDBOX_BASE_URL, PochtaClient, pochta_error, user_key
from aerogram.carriers.pochta.mapping import (
    DEFAULT_PRODUCTS,
    PRODUCTS,
    RATE_FIELDS,
    VAT_RATE_PERCENT,
    components_sum,
    dimension_block,
    mass_grams,
    money_from_rate,
    total_price,
    vat_reading,
)
from aerogram.carriers.pochta.quotes import (
    TARIFF_PATH,
    build_tariff_payload,
    parse_tariff,
    products_for,
)
from aerogram.main import _register_carriers
from aerogram.shared.enums import CargoType, PriceSource
from aerogram.shared.errors import (
    CarrierAuthError,
    CarrierError,
    CarrierTimeout,
    CarrierValidationError,
)
from aerogram.shared.money import Money

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pochta"


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _account(mode: str = "own_contract", **overrides: object) -> CarrierAccount:
    defaults: dict[str, object] = {
        "account_id": "acc-1",
        "carrier_code": "pochta",
        "mode": mode,
        "credentials": {"token": "app-token", "user_key": "bG9naW46cGFzc3dvcmQ="},
    }
    defaults.update(overrides)
    return CarrierAccount(**defaults)  # type: ignore[arg-type]


def _request(**overrides: object) -> QuoteRequest:
    defaults: dict[str, object] = {
        "sender": Party(city_fias_id=None, city_name="Москва", postal_code="101000"),
        "recipient": Party(city_fias_id=None, city_name="Владивосток", postal_code="690000"),
        "places": (Place(weight_kg=Decimal("1.234"), length_cm=30, width_cm=20, height_cm=15),),
        "declared_value": Money.from_major("15000", "RUB"),
        "cargo_type": CargoType.PARCEL,
        "pickup": False,
        "delivery_to_door": True,
    }
    defaults.update(overrides)
    return QuoteRequest(**defaults)  # type: ignore[arg-type]


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> PochtaAdapter:
    def factory(acc: CarrierAccount) -> PochtaClient:
        inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SANDBOX_BASE_URL)
        return PochtaClient(
            token=acc.credentials["token"],
            user_auth_key=acc.credentials.get("user_key", "key"),
            http_client=inner,
        )

    return PochtaAdapter(client_factory=factory)


class TestUnits:
    """Единицы Почты: граммы и сантиметры, целыми числами."""

    def test_mass_is_whole_grams_rounded_up(self) -> None:
        # Вниз округлять нельзя: счёт придёт по весу перевозчика, и разницу
        # заплатит клиент.
        assert mass_grams(Decimal("1.234")) == 1234
        assert mass_grams(Decimal("1.2341")) == 1235
        assert mass_grams(Decimal("0.0001")) == 1

    def test_mass_never_zero(self) -> None:
        # Ноль граммов — не отправление, а отказ расчёта на стороне Почты.
        assert mass_grams(Decimal("0")) == 1

    def test_dimension_is_centimetres_as_is(self) -> None:
        place = Place(weight_kg=Decimal("1"), length_cm=30, width_cm=20, height_cm=15)
        assert dimension_block(place) == {"length": 30, "width": 20, "height": 15}


class TestMoney:
    """Деньги: копейки как есть, НДС складывается ровно в одном месте."""

    def test_response_is_already_minor_units(self) -> None:
        # «Возвращаемые значения указываются в копейках» — делить нельзя.
        assert total_price(load("tariff_ok")) == Money(32_800 + 7_216, "RUB")

    def test_component_carries_its_own_vat(self) -> None:
        assert money_from_rate({"rate": 28_900, "vat": 6_358}) == Money(35_258, "RUB")

    def test_missing_vat_reads_as_zero(self) -> None:
        assert money_from_rate({"rate": 28_900}) == Money(28_900, "RUB")

    def test_missing_component_is_none_not_zero(self) -> None:
        # Ноль означал бы «услуга бесплатна»; её просто не считали.
        assert money_from_rate(None) is None
        assert money_from_rate({"vat": 100}) is None

    def test_true_is_not_one_kopeck(self) -> None:
        # bool в Python — подкласс int, и True молча стал бы копейкой.
        assert money_from_rate({"rate": True}) is None
        assert total_price({"total-rate": True}) is None

    def test_answer_without_total_rate_is_not_an_offer(self) -> None:
        assert total_price(load("tariff_without_price")) is None


class TestVatReading:
    """Судьба НДС: ответ объясняет себя сам, а не выбирается догадкой.

    Страница расчёта не говорит, включает ли «Плата всего» налог, и оба
    чтения опираются на источник: внутри ответа `rate` это «Тариф без НДС»,
    а во всей остальной справке сумма без налога помечена суффиксом —
    «total-rate-wo-vat — Плата всего без НДС». Спор решается арифметикой:
    в ответе есть и составляющие, и итог.
    """

    def test_total_equal_to_components_proves_vat_is_added_on_top(self) -> None:
        body = {
            "ground-rate": {"rate": 10_000, "vat": 2_200},
            "total-rate": 10_000,
            "total-vat": 2_200,
        }
        assert vat_reading(body) == "excluded"
        assert total_price(body) == Money(12_200, "RUB")

    def test_total_equal_to_components_with_vat_proves_it_is_inside(self) -> None:
        # То же самое, но итог уже содержит налог: складывать нельзя,
        # иначе цена Почты завышена на 22 %.
        body = {
            "ground-rate": {"rate": 10_000, "vat": 2_200},
            "total-rate": 12_200,
            "total-vat": 2_200,
        }
        assert vat_reading(body) == "included"
        assert total_price(body) == Money(12_200, "RUB")

    def test_the_fixture_answer_proves_vat_is_on_top(self) -> None:
        assert vat_reading(load("tariff_ok")) == "excluded"

    def test_components_without_vat_prove_nothing(self) -> None:
        # При нулевом налоге в составляющих обе проверки совпали бы,
        # и любой ответ «доказывал» бы что угодно.
        body = {"ground-rate": {"rate": 10_000}, "total-rate": 10_000, "total-vat": 0}
        assert vat_reading(body) == "inconclusive"

    def test_approximate_match_is_not_a_match(self) -> None:
        # Приблизительное совпадение значит, что мы чего-то не понимаем
        # в ответе; округлить до удобного вывода хуже, чем сказать «не знаю».
        # Составляющие тут не решают ничего — дальше слово за ставкой,
        # и в этом ответе она тоже молчит.
        body = {
            "ground-rate": {"rate": 10_000, "vat": 1_500},
            "total-rate": 10_001,
            "total-vat": 1_500,
        }
        assert vat_reading(body) == "inconclusive"

    def test_when_components_disagree_the_rate_still_speaks(self) -> None:
        # Порядок именно такой: сперва точная арифметика, потом ставка.
        # Иначе несошедшийся на копейку ответ терял бы и второй способ.
        body = {
            "ground-rate": {"rate": 10_000, "vat": 2_200},
            "total-rate": 10_001,
            "total-vat": 2_200,
        }
        assert vat_reading(body) == "excluded"

    def test_the_rate_decides_when_components_are_absent(self) -> None:
        # 22 % сверху — доля больше, чем 22/122, значит налог начислен сверх.
        assert vat_reading({"total-rate": 10_000, "total-vat": 2_200}) == "excluded"

    def test_the_rate_cannot_prove_the_opposite(self) -> None:
        # Доля ровно как у налога внутри суммы; но так же выглядит и ответ,
        # где часть услуг от НДС освобождена. Доказательства нет.
        assert vat_reading({"total-rate": 12_200, "total-vat": 2_200}) == "inconclusive"

    def test_inconclusive_falls_back_to_adding(self) -> None:
        # Осторожная сторона: при ошибке в неё Почта проигрывает сравнение,
        # которое должна была выиграть, а при ошибке в другую Decision Engine
        # порекомендовал бы её ошибочно.
        assert total_price({"total-rate": 12_200, "total-vat": 2_200}) == Money(14_400, "RUB")

    def test_partial_exemption_stays_inconclusive(self) -> None:
        assert vat_reading({"total-rate": 10_000, "total-vat": 500}) == "inconclusive"

    def test_absent_vat_decides_nothing(self) -> None:
        assert vat_reading({"total-rate": 10_000, "total-vat": 0}) == "inconclusive"
        assert vat_reading({}) == "inconclusive"

    def test_the_rate_is_a_single_constant(self) -> None:
        # Справочник самой Почты ставки не знает: он держит исторические коды
        # и дописывает актуальную в скобках. Значит она наша, и она одна.
        assert int(VAT_RATE_PERCENT) == 22

    def test_components_are_summed_over_every_documented_field(self) -> None:
        # Пропущенное поле сделало бы сверку слепой: итог перестал бы
        # сходиться с составляющими, и трактовка молча ушла бы в умолчание.
        body: dict[str, object] = {field: {"rate": 100, "vat": 22} for field in RATE_FIELDS}
        assert components_sum(body) == (100 * len(RATE_FIELDS), 22 * len(RATE_FIELDS))

    def test_a_component_without_rate_is_not_counted(self) -> None:
        assert components_sum({"ground-rate": {"vat": 22}, "avia-rate": None}) == (0, 0)


class TestPayload:
    """Тело запроса: обязательные поля, индексы и одно место."""

    def test_required_fields_are_always_present(self) -> None:
        payload = build_tariff_payload(_request(), PRODUCTS["POSTAL_PARCEL:SURFACE"])
        for field in (
            "mail-type",
            "mail-category",
            "mass",
            "inventory",
            "with-order-of-notice",
            "with-simple-notice",
        ):
            assert field in payload, field
        # Платных услуг мы не заказываем — явное «нет», а не отсутствие поля.
        assert payload["inventory"] is False
        assert payload["with-order-of-notice"] is False
        assert payload["with-simple-notice"] is False

    def test_units_reach_the_body_as_the_carrier_declares_them(self) -> None:
        # Проверяются значения, а не наличие ключей: перепутанная единица
        # проходит любую проверку на ключи и стоит тысячекратной ошибки.
        payload = build_tariff_payload(_request(), PRODUCTS["POSTAL_PARCEL:SURFACE"])
        assert payload["mass"] == 1234  # 1,234 кг в целых граммах
        assert payload["dimension"] == {"length": 30, "width": 20, "height": 15}
        assert payload["transport-type"] == "SURFACE"
        assert payload["mail-type"] == "POSTAL_PARCEL"
        assert build_tariff_payload(_request(), PRODUCTS["EMS:EXPRESS"])["transport-type"] == (
            "EXPRESS"
        )

    def test_indexes_go_as_strings_both_ways(self) -> None:
        # Ведущий ноль значим, поэтому индекс — строка, а не число.
        payload = build_tariff_payload(_request(), PRODUCTS["POSTAL_PARCEL:SURFACE"])
        assert payload["index-from"] == "101000"
        assert payload["index-to"] == "690000"

    def test_sender_index_is_never_left_to_the_carrier_profile(self) -> None:
        # Без index-from Почта берёт индекс из профиля клиента в её кабинете:
        # при мультиарендности это тихая зависимость цены от чужой настройки.
        request = _request(sender=Party(city_fias_id=None, city_name="Москва", postal_code=None))
        with pytest.raises(CarrierValidationError):
            build_tariff_payload(request, PRODUCTS["POSTAL_PARCEL:SURFACE"])

    def test_without_recipient_index_there_is_no_quote(self) -> None:
        request = _request(
            recipient=Party(city_fias_id=None, city_name="Владивосток", postal_code=None)
        )
        with pytest.raises(CarrierValidationError):
            build_tariff_payload(request, PRODUCTS["POSTAL_PARCEL:SURFACE"])

    def test_several_places_are_refused_before_the_call(self) -> None:
        # Одно РПО — одно место: в теле расчёта одна mass и один dimension.
        place = Place(weight_kg=Decimal("1"), length_cm=10, width_cm=10, height_cm=10)
        with pytest.raises(CarrierValidationError):
            build_tariff_payload(_request(places=(place, place)), PRODUCTS["EMS:EXPRESS"])

    def test_insurance_switches_category_and_adds_declared_value(self) -> None:
        # У Почты объявленная ценность — это категория РПО, отдельного флага
        # страхования в теле расчёта нет.
        payload = build_tariff_payload(_request(insurance=True), PRODUCTS["POSTAL_PARCEL:SURFACE"])
        assert payload["mail-category"] == "WITH_DECLARED_VALUE"
        assert payload["declared-value"] == 1_500_000

    def test_without_insurance_declared_value_is_not_sent(self) -> None:
        payload = build_tariff_payload(_request(), PRODUCTS["POSTAL_PARCEL:SURFACE"])
        assert payload["mail-category"] == "ORDINARY"
        assert "declared-value" not in payload

    def test_entries_type_is_sent_only_when_asked(self) -> None:
        # Поле обязательно по таблице, но описано как «для международных
        # отправлений». Подставлять категорию вложения внутренней посылке
        # было бы выдумкой — см. quotes.
        assert "entries-type" not in build_tariff_payload(
            _request(), PRODUCTS["POSTAL_PARCEL:SURFACE"]
        )
        payload = build_tariff_payload(
            _request(extras={"entries_type": "GIFT"}), PRODUCTS["POSTAL_PARCEL:SURFACE"]
        )
        assert payload["entries-type"] == "GIFT"


class TestProducts:
    """Набор продуктов: один запрос — одна цена, значит набор виден явно."""

    def test_default_set_is_used_when_nothing_asked(self) -> None:
        codes = [p.code for p in products_for(_request())]
        assert codes == list(DEFAULT_PRODUCTS)

    def test_caller_may_narrow_the_set(self) -> None:
        products = products_for(_request(extras={"products": ["EMS:EXPRESS"]}))
        assert [p.code for p in products] == ["EMS:EXPRESS"]

    def test_unknown_product_falls_back_instead_of_guessing(self) -> None:
        # Молча посчитать не то, что просили, выглядит как «Почта подешевела».
        products = products_for(_request(extras={"products": ["ПОСЫЛОЧКА"]}))
        assert [p.code for p in products] == list(DEFAULT_PRODUCTS)


class TestParse:
    """Разбор ответа: цена, срок и расшифровка."""

    def test_full_answer_becomes_one_offer(self) -> None:
        quote = parse_tariff(
            load("tariff_ok"),
            PRODUCTS["POSTAL_PARCEL:SURFACE"],
            price_source=PriceSource.OWN_CONTRACT,
        )
        assert quote is not None
        assert quote.price == Money(40_016, "RUB")
        assert (quote.transit_days_min, quote.transit_days_max) == (4, 9)
        assert quote.service_name == "Посылка нестандартная, наземная"
        assert quote.price_breakdown["Пересылка"] == Money(35_258, "RUB")
        assert quote.price_breakdown["Объявленная ценность"] == Money(1_830, "RUB")

    def test_answer_without_delivery_time_still_has_a_price(self) -> None:
        # Блок срока помечен опциональным целиком.
        quote = parse_tariff(
            load("tariff_no_delivery_time"),
            PRODUCTS["POSTAL_PARCEL:SURFACE"],
            price_source=PriceSource.AEROGRAM,
        )
        assert quote is not None
        assert (quote.transit_days_min, quote.transit_days_max) == (0, 0)
        assert quote.promised_delivery_date is None

    def test_only_max_days_makes_a_point_not_a_range(self) -> None:
        # Вилка «от нуля» обещала бы доставку сегодня и выиграла бы
        # любое сравнение по скорости.
        quote = parse_tariff(
            load("tariff_avia"), PRODUCTS["EMS:EXPRESS"], price_source=PriceSource.AEROGRAM
        )
        assert quote is not None
        assert (quote.transit_days_min, quote.transit_days_max) == (3, 3)
        assert quote.promised_delivery_date is not None

    def test_only_min_days_is_a_point_too(self) -> None:
        body = {"total-rate": 1000, "total-vat": 220, "delivery-time": {"min-days": 5}}
        quote = parse_tariff(body, PRODUCTS["EMS:EXPRESS"], price_source=PriceSource.AEROGRAM)
        assert quote is not None
        assert (quote.transit_days_min, quote.transit_days_max) == (5, 5)

    def test_an_inverted_range_collapses_to_the_maximum(self) -> None:
        # Вилка «от 9 до 3» — не вилка. Взять из неё минимум значило бы
        # обещать срок, которого перевозчик не называл.
        body = {
            "total-rate": 1000,
            "total-vat": 220,
            "delivery-time": {"min-days": 9, "max-days": 3},
        }
        quote = parse_tariff(body, PRODUCTS["EMS:EXPRESS"], price_source=PriceSource.AEROGRAM)
        assert quote is not None
        assert (quote.transit_days_min, quote.transit_days_max) == (3, 3)

    def test_unparsable_total_vat_is_not_zero(self) -> None:
        # Отсутствующий налог — ноль, непрочитанный — нет: подставив ноль,
        # мы занизили бы цену ровно на его величину.
        assert total_price({"total-rate": 10_000, "total-vat": "пусто"}) is None
        assert total_price({"total-rate": 10_000}) == Money(10_000, "RUB")

    def test_answer_without_price_is_not_an_offer(self) -> None:
        assert (
            parse_tariff(
                load("tariff_without_price"),
                PRODUCTS["EMS:EXPRESS"],
                price_source=PriceSource.AEROGRAM,
            )
            is None
        )


class TestErrors:
    """Ошибка перевозчика: разбирается, но никогда не даёт 500."""

    def test_both_error_envelopes_are_recognised(self) -> None:
        assert pochta_error(load("error_tariff")) == "ILLEGAL_INDEX_TO Почтовый индекс некорректен"
        assert "EMPTY_MAIL_TYPE" in (pochta_error(load("error_batch_style")) or "")

    def test_bare_string_code_is_an_error_too(self) -> None:
        # Часть методов отвечает голой строкой кода.
        assert pochta_error('"SESSION_IN_PROGRESS"') == "SESSION_IN_PROGRESS"

    def test_undefined_placeholder_is_not_an_error(self) -> None:
        # `UNDEFINED` — значение-заглушка из схем документации, оно приходит
        # и в успешных ответах.
        assert pochta_error({"error-code": "UNDEFINED", "f103-sent": True}) is None

    def test_successful_answer_has_no_error(self) -> None:
        assert pochta_error(load("tariff_ok")) is None

    @pytest.mark.anyio
    async def test_carrier_error_does_not_become_500(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=load("error_tariff"))

        adapter = _adapter(handler)
        with pytest.raises(CarrierError):
            await adapter.quote(_request(), _account())


class TestClient:
    """Заголовки авторизации и адрес API."""

    @pytest.mark.anyio
    async def test_two_headers_with_their_own_prefixes(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json=load("tariff_ok"))

        client = PochtaClient(
            token="app-token",
            user_auth_key="bG9naW46cGFzc3dvcmQ=",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url=SANDBOX_BASE_URL
            ),
        )
        await client.post(TARIFF_PATH, {}, operation="quote")
        assert seen["authorization"] == "AccessToken app-token"
        assert seen["x-user-authorization"] == "Basic bG9naW46cGFzc3dvcmQ="
        # Кодировка объявлена перевозчиком явно и на каждой странице справки.
        assert seen["content-type"] == "application/json;charset=UTF-8"

    def test_user_key_is_computed_from_the_pair(self) -> None:
        # «base64(login:password) = bG9naW46cGFzc3dvcmQ=» — пример самой Почты.
        assert user_key(login="login", password="password", key=None) == "bG9naW46cGFzc3dvcmQ="

    def test_ready_key_wins_over_the_pair(self) -> None:
        # У тенанта может не быть исходного пароля под рукой.
        assert user_key(login="l", password="p", key="ready") == "ready"

    def test_without_key_and_pair_there_is_no_client(self) -> None:
        with pytest.raises(CarrierValidationError):
            user_key(login=None, password=None, key=None)

    def test_missing_token_is_refused_at_construction(self) -> None:
        with pytest.raises(CarrierValidationError):
            PochtaClient(token="", user_auth_key="key")

    def test_sandbox_uses_the_only_address_the_carrier_published(self) -> None:
        client = PochtaClient(token="t", user_auth_key="k", is_sandbox=True)
        assert client.base_url == SANDBOX_BASE_URL

    def test_production_without_an_address_is_refused(self) -> None:
        # Боевого адреса Почта в документации не публикует, и подставлять
        # выдуманный запрещает планка ADR-0020.
        with pytest.raises(CarrierValidationError):
            PochtaClient(token="t", user_auth_key="k", is_sandbox=False)

    def test_configured_address_wins(self) -> None:
        client = PochtaClient(
            token="t",
            user_auth_key="k",
            base_url="https://otpravka-api.example/",
            is_sandbox=False,
        )
        assert client.base_url == "https://otpravka-api.example"


class TestAdapter:
    """Путь целиком: один запрос на продукт."""

    @pytest.mark.anyio
    async def test_quote_asks_once_per_product(self) -> None:
        asked: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == TARIFF_PATH
            asked.append(json.loads(request.content))
            return httpx.Response(200, json=load("tariff_ok"))

        quotes = await _adapter(handler).quote(_request(), _account())
        assert len(asked) == len(DEFAULT_PRODUCTS)
        assert {q.service_code for q in quotes} == set(DEFAULT_PRODUCTS)
        assert {p["mail-type"] for p in asked} == {"POSTAL_PARCEL", "EMS"}

    @pytest.mark.anyio
    async def test_price_source_follows_the_contract_mode(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=load("tariff_ok"))

        quotes = await _adapter(handler).quote(
            _request(extras={"products": ["EMS:EXPRESS"]}), _account(mode="aerogram")
        )
        assert quotes[0].price_source is PriceSource.AEROGRAM

    @pytest.mark.anyio
    async def test_a_refused_product_does_not_kill_the_others(self) -> None:
        # Сочетаемость видов РПО с категориями нигде не документирована,
        # поэтому отказ по одному сочетанию — ожидаемый исход. Раньше он
        # уносил всю выдачу по Почте: исключение летело из цикла наружу.
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["mail-type"] == "EMS":
                return httpx.Response(200, json=load("error_tariff"))
            return httpx.Response(200, json=load("tariff_ok"))

        quotes = await _adapter(handler).quote(_request(), _account())
        assert [q.service_code for q in quotes] == ["POSTAL_PARCEL:SURFACE"]

    @pytest.mark.anyio
    async def test_when_every_product_is_refused_the_reason_survives(self) -> None:
        # Пустой список сказал бы «Почта не возит по этому направлению»
        # там, где она сказала почему.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=load("error_tariff"))

        with pytest.raises(CarrierError) as exc:
            await _adapter(handler).quote(_request(), _account())
        assert "ILLEGAL_INDEX_TO" in str(exc.value)

    @pytest.mark.anyio
    async def test_an_account_wide_failure_is_raised_at_once(self) -> None:
        # Неверный токен одинаков для всех продуктов: повторять запрос
        # по каждому значит тратить суточную квоту на тот же ответ.
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(401, json={"error-code": "AUTH"})

        with pytest.raises(CarrierAuthError):
            await _adapter(handler).quote(_request(), _account())
        assert len(calls) == 1

    @pytest.mark.anyio
    async def test_declared_value_in_another_currency_is_refused(self) -> None:
        # Почта прочтёт сумму как рубли, а страховой сбор считается
        # процентом от неё: ошибка попадёт и в цену, и в фактический счёт.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=load("tariff_ok"))

        request = _request(insurance=True, declared_value=Money.from_major("150", "USD"))
        with pytest.raises(CarrierValidationError):
            await _adapter(handler).quote(request, _account())

    @pytest.mark.anyio
    async def test_product_without_price_does_not_kill_the_others(self) -> None:
        # Сочетаемость видов РПО с категориями нигде не документирована,
        # поэтому отказ по одному продукту — ожидаемый исход, а не сбой.
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["mail-type"] == "EMS":
                return httpx.Response(200, json=load("tariff_without_price"))
            return httpx.Response(200, json=load("tariff_ok"))

        quotes = await _adapter(handler).quote(_request(), _account())
        assert [q.service_code for q in quotes] == ["POSTAL_PARCEL:SURFACE"]


class TestDefaultClient:
    """Боевая сборка клиента: то, что в тестах обычно подменяется.

    Подменяя фабрику во всех остальных тестах, легко не заметить, что
    настоящая сборка не работает вовсе: у неё свои проверки и свой разбор
    учётной записи.
    """

    @staticmethod
    def _acc(**overrides: object) -> CarrierAccount:
        defaults: dict[str, object] = {
            "account_id": "acc-1",
            "carrier_code": "pochta",
            "mode": "own_contract",
            "credentials": {"token": "t", "user_key": "k"},
        }
        defaults.update(overrides)
        return CarrierAccount(**defaults)  # type: ignore[arg-type]

    def test_the_configured_address_is_used(self) -> None:
        client = PochtaAdapter._default_client(
            self._acc(is_sandbox=False, settings={"base_url": "https://api.example/"})
        )
        assert client.base_url == "https://api.example"

    def test_production_without_an_address_is_refused(self) -> None:
        with pytest.raises(CarrierValidationError):
            PochtaAdapter._default_client(self._acc(is_sandbox=False))

    def test_the_pair_replaces_the_ready_key(self) -> None:
        client = PochtaAdapter._default_client(
            self._acc(credentials={"token": "t", "login": "login", "password": "password"})
        )
        assert client.base_url == SANDBOX_BASE_URL

    def test_a_missing_token_is_refused(self) -> None:
        with pytest.raises(CarrierValidationError):
            PochtaAdapter._default_client(self._acc(credentials={"user_key": "k"}))

    def test_a_token_alone_is_refused(self) -> None:
        # Ключ пользователя не собрать ни из чего: расчёт упал бы на первом
        # вызове, то есть ошибка нашлась бы у клиента, а не в кабинете.
        with pytest.raises(CarrierValidationError):
            PochtaAdapter._default_client(self._acc(credentials={"token": "t"}))


class TestBaseUrlIsChecked:
    """Настройка учётной записи решает, кому достанутся секреты тенанта."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.example",  # открытое соединение
            "https://someone:secret@api.example",  # встроенные учётные данные
            "https:///1.0",  # без хоста
            "https://api.example/?to=evil",  # параметры запроса
        ],
    )
    def test_a_dangerous_address_is_refused(self, url: str) -> None:
        with pytest.raises(CarrierValidationError):
            PochtaClient(token="t", user_auth_key="k", base_url=url, is_sandbox=False)

    def test_a_good_address_passes(self) -> None:
        client = PochtaClient(
            token="t", user_auth_key="k", base_url="https://api.example/1.0/", is_sandbox=False
        )
        assert client.base_url == "https://api.example/1.0"


class TestSecretsDoNotLeak:
    """Страж: секреты не попадают в снимок вызова.

    Снимок уходит в `carrier_raw_calls` и живёт 30 суток. Такой тест дешевле
    любого ревью, потому что срабатывает у того, кто добавит заголовки
    в снимок, не подумав.
    """

    @pytest.mark.anyio
    async def test_neither_token_nor_key_reach_the_raw_call(self) -> None:
        seen: list[object] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=load("tariff_ok"))

        async def on_raw_call(call: object) -> None:
            seen.append(call)

        client = PochtaClient(
            token="secret-token",
            user_auth_key="secret-key",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url=SANDBOX_BASE_URL
            ),
        )
        await client.post(TARIFF_PATH, {"mass": 1}, operation="quote", on_raw_call=on_raw_call)
        assert seen
        dumped = repr(seen[0])
        assert "secret-token" not in dumped
        assert "secret-key" not in dumped


class TestCapabilities:
    """Возможности объявлены по источнику, а не по удобству."""

    def test_what_the_carrier_cannot_do(self) -> None:
        caps = PochtaAdapter.capabilities
        # Отмены нет ни в одном из методов справки (ADR-0020, решение 4).
        assert caps.supports_cancel is False
        # Ни приёма события, ни подписки на него в справке нет.
        assert caps.supports_webhooks is False
        # Метода подачи заявки на курьера в API «Отправка» нет.
        assert caps.supports_pickup_request is False

    def test_what_it_can(self) -> None:
        caps = PochtaAdapter.capabilities
        # Объявленная ценность — категория РПО плюс declared-value.
        assert caps.supports_insurance is True
        # Одно РПО — одно место: в теле расчёта одна mass и один dimension.
        assert caps.max_places == 1
        # Флаг означает «платформа не досчитывает объёмный вес». Почта
        # тарифицирует по фактической массе, и подмена веса объёмным
        # отправила бы ей массу, которой не существует.
        assert caps.computes_volumetric_weight is True

    def test_the_adapter_satisfies_the_protocol(self) -> None:
        assert isinstance(PochtaAdapter(), CarrierAdapter)

    def test_it_is_registered_under_its_own_code(self) -> None:
        # Иначе домен не найдёт перевозчика через реестр, а import-linter
        # промолчит: контракт запрещает прямой импорт, а не забытую запись.
        registry._reset_for_tests()
        _register_carriers()
        assert POCHTA_CODE in registry.available_codes()
        assert registry.get_adapter(POCHTA_CODE).name == "Почта России"


class TestClientFailures:
    """Сбойные ветки транспорта: ни одна не должна дать 500."""

    @staticmethod
    def _client(handler: object) -> PochtaClient:
        return PochtaClient(
            token="t",
            user_auth_key="k",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
                base_url=SANDBOX_BASE_URL,
            ),
        )

    @pytest.mark.anyio
    async def test_html_instead_of_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>502 Bad Gateway</html>")

        with pytest.raises(CarrierError):
            await self._client(handler).post(TARIFF_PATH, {}, operation="quote")

    @pytest.mark.anyio
    async def test_empty_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        with pytest.raises(CarrierError):
            await self._client(handler).post(TARIFF_PATH, {}, operation="quote")

    @pytest.mark.anyio
    async def test_a_list_of_errors_at_the_top_level(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"error-code": "EMPTY_MAIL_TYPE"}])

        with pytest.raises(CarrierError) as exc:
            await self._client(handler).post(TARIFF_PATH, {}, operation="quote")
        assert "EMPTY_MAIL_TYPE" in str(exc.value)

    @pytest.mark.anyio
    async def test_a_bare_list_is_not_a_quote(self) -> None:
        # Список без ошибок телом расчёта быть не может: выдать его за успех
        # значило бы вернуть предложение без цены.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        with pytest.raises(CarrierError):
            await self._client(handler).post(TARIFF_PATH, {}, operation="quote")

    @pytest.mark.anyio
    async def test_a_timeout_is_a_carrier_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        with pytest.raises(CarrierTimeout):
            await self._client(handler).post(TARIFF_PATH, {}, operation="quote")
