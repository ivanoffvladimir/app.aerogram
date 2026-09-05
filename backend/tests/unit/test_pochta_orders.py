"""Оформление у Почты России: заказ, сверка «призраков» и форма Ф7п.

Фикстуры синтетические, как и у расчёта: примеров ответа справка Почты
не содержит вовсе, есть только структура полей. Что это доказывает, а что
нет, написано в `tests/fixtures/pochta/README.md`.

Сеть не используется: транспорт подменяется целиком.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from aerogram.carriers.base import (
    CarrierAccount,
    Party,
    Place,
    ShipmentRequest,
)
from aerogram.carriers.pochta.adapter import PochtaAdapter
from aerogram.carriers.pochta.client import SANDBOX_BASE_URL, PochtaClient
from aerogram.carriers.pochta.mapping import PRODUCTS
from aerogram.carriers.pochta.orders import (
    BACKLOG_PATH,
    RUSSIA_COUNTRY_CODE,
    SEARCH_PATH,
    create_payload,
    form_path,
    parse_created,
    parse_found,
)
from aerogram.shared.enums import CargoType, LabelFormat
from aerogram.shared.errors import CarrierError, CarrierValidationError
from aerogram.shared.money import Money

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pochta"

NUMBER = "AG-2026-000123"

#: Части адреса, которых требует Почта и не носит наш `Party`.
ADDRESS_PARTS: dict[str, object] = {
    "region": "Приморский край",
    "street": "Светланская",
    "house": "10",
}

PARCEL = PRODUCTS["POSTAL_PARCEL:SURFACE"]


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _account(**overrides: object) -> CarrierAccount:
    defaults: dict[str, object] = {
        "account_id": "acc-1",
        "carrier_code": "pochta",
        "mode": "own_contract",
        "credentials": {"token": "app-token", "user_key": "bG9naW46cGFzc3dvcmQ="},
        "settings": {},
    }
    defaults.update(overrides)
    return CarrierAccount(**defaults)  # type: ignore[arg-type]


def _shipment(**overrides: object) -> ShipmentRequest:
    defaults: dict[str, object] = {
        "number": NUMBER,
        "service_code": PARCEL.code,
        "tariff_code": PARCEL.code,
        "sender": Party(city_fias_id=None, city_name="Москва", postal_code="101000"),
        "recipient": Party(
            city_fias_id=None,
            city_name="Владивосток",
            postal_code="690000",
            contact_person="Иванов Иван Иванович",
            phone="+7 (900) 123-45-67",
        ),
        "places": (Place(weight_kg=Decimal("1.234"), length_cm=30, width_cm=20, height_cm=15),),
        "declared_value": Money.from_major("15000", "RUB"),
        "cargo_type": CargoType.PARCEL,
        "pickup": False,
        "delivery_to_door": True,
        "extras": dict(ADDRESS_PARTS),
    }
    defaults.update(overrides)
    return ShipmentRequest(**defaults)  # type: ignore[arg-type]


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> PochtaAdapter:
    def factory(acc: CarrierAccount) -> PochtaClient:
        inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SANDBOX_BASE_URL)
        return PochtaClient(
            token=acc.credentials["token"],
            user_auth_key=acc.credentials.get("user_key", "key"),
            http_client=inner,
        )

    return PochtaAdapter(client_factory=factory)


def _order(**overrides: object) -> dict[str, Any]:
    """Тело одного заказа из собранной пачки."""
    payload = create_payload(_shipment(**overrides), PARCEL, sender_index=None)
    assert len(payload) == 1
    return payload[0]


class TestCreatePayload:
    """Тело заказа: единицы, обязательные поля и наш номер внутри."""

    def test_one_order_per_call(self) -> None:
        # Метод принимает пачку, мы кладём один заказ: групповая отправка —
        # это наши массовые отправления, а не пачка внутри одного вызова.
        assert len(create_payload(_shipment(), PARCEL, sender_index=None)) == 1

    def test_required_fields_are_present(self) -> None:
        order = _order()
        for field in (
            "order-num",
            "mail-type",
            "mail-category",
            "mail-direct",
            "mass",
            "address-type-to",
            "index-to",
            "region-to",
            "place-to",
            "street-to",
            "house-to",
            "recipient-name",
            "surname",
            "given-name",
            "postoffice-code",
            "tariff-count",
        ):
            assert field in order, field

    def test_units_reach_the_body_as_the_carrier_declares_them(self) -> None:
        # Проверяются значения, а не наличие ключей: перепутанная единица
        # проходит любую проверку на ключи и стоит тысячекратной ошибки.
        order = _order()
        assert order["mass"] == 1234  # 1,234 кг в целых граммах
        assert order["dimension"] == {"length": 30, "width": 20, "height": 15}
        assert order["mail-direct"] == RUSSIA_COUNTRY_CODE == 643

    def test_our_number_goes_to_the_carrier(self) -> None:
        # По нему потом идёт сверка «призраков» (FR-2.5). Без него сверять
        # нечем, и после потерянного ответа мы создали бы второй заказ.
        assert _order()["order-num"] == NUMBER

    def test_recipient_index_is_an_integer_here(self) -> None:
        # В расчёте то же поле объявлено строкой. Расхождение принадлежит
        # контракту перевозчика, и оно зафиксировано тестом, чтобы правка
        # «для единообразия» не сломала оформление.
        order = _order()
        assert order["index-to"] == 690000
        assert isinstance(order["index-to"], int)

    def test_product_reaches_the_order_the_way_it_was_priced(self) -> None:
        order = _order()
        assert order["mail-type"] == "POSTAL_PARCEL"
        assert order["mail-category"] == "ORDINARY"
        assert order["transport-type"] == "SURFACE"
        express = create_payload(
            _shipment(tariff_code="EMS:EXPRESS"), PRODUCTS["EMS:EXPRESS"], sender_index=None
        )[0]
        assert express["mail-type"] == "EMS"
        assert express["transport-type"] == "EXPRESS"

    def test_full_tariff_only(self) -> None:
        # 1 — только полный тариф. Скидочный нам не с чем сверять, а выбор
        # между ними был бы решением о цене, которого никто не принимал.
        assert _order()["tariff-count"] == 1

    def test_paid_services_are_explicit_no(self) -> None:
        order = _order()
        assert order["inventory"] is False
        assert order["with-order-of-notice"] is False
        assert order["with-simple-notice"] is False

    def test_insurance_switches_category_and_sends_kopecks(self) -> None:
        # У Почты объявленная ценность — это категория РПО; сумма в копейках,
        # как её называет остальной контракт («insr-value … (копейки)»).
        order = _order(insurance=True)
        assert order["mail-category"] == "WITH_DECLARED_VALUE"
        assert order["insr-value"] == 1_500_000

    def test_without_insurance_declared_value_is_not_sent(self) -> None:
        order = _order()
        assert order["mail-category"] == "ORDINARY"
        assert "insr-value" not in order

    def test_phone_goes_as_digits_only(self) -> None:
        # Поле объявлено целым числом: «+7 (900) …» перевозчик не примет.
        assert _order()["tel-address"] == 9001234567

    def test_flat_becomes_room(self) -> None:
        assert "room-to" not in _order()
        order = _order(extras={**ADDRESS_PARTS, "flat": "12"})
        assert order["room-to"] == "12"


class TestCreateRefusesRatherThanGuesses:
    """Отказ до вызова там, где догадка отправила бы груз не туда."""

    @pytest.mark.parametrize("missing", ["region", "street", "house"])
    def test_without_an_address_part_there_is_no_order(self, missing: str) -> None:
        # Разбирать адрес одной строкой регулярным выражением здесь нельзя:
        # ошибка разбора отправляет груз к другому дому, и молча.
        extras = {key: value for key, value in ADDRESS_PARTS.items() if key != missing}
        with pytest.raises(CarrierValidationError) as exc:
            _order(extras=extras)
        assert exc.value.field == missing

    def test_without_recipient_index_there_is_no_order(self) -> None:
        recipient = Party(city_fias_id=None, city_name="Владивосток", contact_person="Иванов Иван")
        with pytest.raises(CarrierValidationError):
            _order(recipient=recipient)

    def test_a_non_numeric_index_is_refused(self) -> None:
        recipient = Party(
            city_fias_id=None,
            city_name="Владивосток",
            postal_code="69000A",
            contact_person="Иванов Иван",
        )
        with pytest.raises(CarrierValidationError):
            _order(recipient=recipient)

    def test_several_places_are_refused_before_the_call(self) -> None:
        place = Place(weight_kg=Decimal("1"), length_cm=10, width_cm=10, height_cm=10)
        with pytest.raises(CarrierValidationError):
            _order(places=(place, place))

    def test_one_word_contact_is_not_a_name(self) -> None:
        # Почте нужны фамилия и имя порознь. «Иванов» — это не пара,
        # и подставить пустое имя значит отправить заказ на отказ.
        recipient = Party(
            city_fias_id=None,
            city_name="Владивосток",
            postal_code="690000",
            contact_person="Иванов",
        )
        with pytest.raises(CarrierValidationError):
            _order(recipient=recipient)

    def test_name_parts_from_extras_win_over_the_guess(self) -> None:
        # Догадка «первое слово — фамилия» верна для русских форм и неверна
        # для остальных. Там, где части имени известны, гадать не нужно.
        order = _order(extras={**ADDRESS_PARTS, "surname": "Ли", "given_name": "Вэй"})
        assert (order["surname"], order["given-name"]) == ("Ли", "Вэй")

    def test_the_guess_is_used_when_nothing_better_is_given(self) -> None:
        order = _order()
        assert (order["surname"], order["given-name"]) == ("Иванов", "Иван")


class TestPostofficeCode:
    """Индекс отделения приёма: свойство договора, а не отправления."""

    def test_account_setting_is_used(self) -> None:
        order = create_payload(_shipment(), PARCEL, sender_index="107140")[0]
        assert order["postoffice-code"] == "107140"

    def test_request_may_override_the_account(self) -> None:
        # Сдача в другое отделение — событие отправления, а не договора.
        request = _shipment(extras={**ADDRESS_PARTS, "postoffice_code": "690091"})
        assert (
            create_payload(request, PARCEL, sender_index="107140")[0]["postoffice-code"] == "690091"
        )

    def test_sender_index_is_the_last_resort(self) -> None:
        assert _order()["postoffice-code"] == "101000"

    def test_without_any_index_the_order_is_refused(self) -> None:
        sender = Party(city_fias_id=None, city_name="Москва")
        with pytest.raises(CarrierValidationError) as exc:
            _order(sender=sender)
        assert exc.value.field == "postoffice_code"


class TestParseCreated:
    """Ответ создания: идентификатор заказа и ШПИ."""

    def test_barcode_arrives_with_the_order(self) -> None:
        result = parse_created(load("order_created"), number=NUMBER)
        # Внутренний идентификатор — то, что принимают печатные формы;
        # ШПИ — то, что увидит клиент на трекинге.
        assert result.external_id == "4815162342"
        assert result.tracking_number == "80098765432109"
        assert result.is_pending is False

    def test_the_carrier_refusal_becomes_its_own_text(self) -> None:
        with pytest.raises(CarrierValidationError) as exc:
            parse_created(load("order_rejected"), number=NUMBER)
        assert "ILLEGAL_INDEX_TO" in str(exc.value)
        assert "Почтовый индекс некорректен" in str(exc.value)

    def test_undefined_is_an_error_code_not_a_placeholder(self) -> None:
        # Справочник `enums-errors` переводит UNDEFINED как «Неопределенная
        # ошибка». Пропустив его, мы выдали бы отказ за успех.
        body = {"errors": [{"error-codes": [{"code": "UNDEFINED"}]}]}
        with pytest.raises(CarrierValidationError) as exc:
            parse_created(body, number=NUMBER)
        assert "UNDEFINED" in str(exc.value)

    def test_an_empty_answer_is_not_a_created_order(self) -> None:
        with pytest.raises(CarrierValidationError):
            parse_created({"orders": []}, number=NUMBER)

    def test_an_order_without_an_id_cannot_be_stored(self) -> None:
        # Без идентификатора нечем ни напечатать форму, ни свериться.
        with pytest.raises(CarrierValidationError):
            parse_created({"orders": [{"barcode": "800"}]}, number=NUMBER)

    def test_an_order_without_a_barcode_is_pending_not_silent(self) -> None:
        # Так отвечает версия 1.0. Отправление без трек-номера выглядит
        # созданным, а показать клиенту нечего.
        result = parse_created({"orders": [{"result-id": 7}]}, number=NUMBER)
        assert result.tracking_number is None
        assert result.is_pending is True


class TestParseFound:
    """Сверка «призраков»: только точное совпадение нашего номера."""

    def test_our_order_is_found(self) -> None:
        result = parse_found(load("search_found"), number=NUMBER)
        assert result is not None
        assert result.external_id == "4815162342"
        assert result.tracking_number == "80098765432109"

    def test_a_longer_number_is_not_our_order(self) -> None:
        # Перевозчик ищет по подстроке. Приняв чужой заказ за свой, сверка
        # объявила бы созданным то, чего мы не создавали.
        assert parse_found(load("search_other_order"), number=NUMBER) is None

    def test_nothing_found_is_none(self) -> None:
        assert parse_found(load("search_empty"), number=NUMBER) is None

    def test_an_unexpected_shape_is_not_a_find(self) -> None:
        assert parse_found({"orders": []}, number=NUMBER) is None


class TestAdapterCreate:
    """Путь создания целиком."""

    @pytest.mark.anyio
    async def test_it_puts_a_list_to_the_backlog(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=load("order_created"))

        result = await _adapter(handler).create(_shipment(), _account())
        assert seen["method"] == "PUT"
        assert seen["path"] == BACKLOG_PATH
        assert isinstance(seen["body"], list)
        assert seen["body"][0]["order-num"] == NUMBER
        assert result.tracking_number == "80098765432109"

    @pytest.mark.anyio
    async def test_the_account_index_reaches_the_order(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)[0])
            return httpx.Response(200, json=load("order_created"))

        account = _account(settings={"postoffice_code": "107140"})
        await _adapter(handler).create(_shipment(), account)
        assert seen[0]["postoffice-code"] == "107140"

    @pytest.mark.anyio
    async def test_an_unknown_product_never_reaches_the_carrier(self) -> None:
        # Цена показана за один продукт — уехать должен он же.
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json=load("order_created"))

        with pytest.raises(CarrierValidationError):
            await _adapter(handler).create(
                _shipment(service_code="ПОСЫЛОЧКА", tariff_code="ПОСЫЛОЧКА"), _account()
            )
        assert called is False

    @pytest.mark.anyio
    async def test_a_carrier_refusal_is_not_a_created_shipment(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=load("order_rejected"))

        with pytest.raises(CarrierValidationError):
            await _adapter(handler).create(_shipment(), _account())


class TestAdapterFindByNumber:
    """Сверка «призраков» через API поиска."""

    @pytest.mark.anyio
    async def test_it_asks_by_our_own_number(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["query"] = request.url.params.get("query")
            return httpx.Response(200, json=load("search_found"))

        result = await _adapter(handler).find_by_number(NUMBER, _account())
        assert seen["path"] == SEARCH_PATH
        assert seen["query"] == NUMBER
        assert result is not None
        assert result.external_id == "4815162342"

    @pytest.mark.anyio
    async def test_goods_inside_the_order_are_not_read_as_an_error(self) -> None:
        # В найденном заказе есть вложенная декларация, а в ней у товара
        # поля `code` и `description` — те же имена, которыми Почта
        # описывает ошибки. Приняв товар за отказ, сверка «призраков»
        # упала бы на успешном ответе, и домен создал бы второй заказ.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=load("search_found"))

        result = await _adapter(handler).find_by_number(NUMBER, _account())
        assert result is not None
        assert result.external_id == "4815162342"

    @pytest.mark.anyio
    async def test_nothing_found_is_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        assert await _adapter(handler).find_by_number(NUMBER, _account()) is None

    @pytest.mark.anyio
    async def test_a_failure_is_never_read_as_not_found(self) -> None:
        # Ложное «не найден» означает второй заказ у перевозчика —
        # со вторым ШПИ, вторым грузом и вторым счётом.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error-code": "INTERNAL"})

        with pytest.raises(CarrierError):
            await _adapter(handler).find_by_number(NUMBER, _account())


class TestAdapterLabel:
    """Форма Ф7п: PDF как есть."""

    @pytest.mark.anyio
    async def test_the_pdf_comes_back_untouched(self) -> None:
        pdf = b"%PDF-1.4\n%\xd0\xa47p\n"
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})

        result = await _adapter(handler).label("4815162342", LabelFormat.PDF_A4, _account())
        assert seen["path"] == form_path("4815162342") == "/1.0/forms/4815162342/f7pdf"
        assert result.content == pdf
        assert result.is_pending is False

    @pytest.mark.anyio
    async def test_another_format_is_refused_before_the_call(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, content=b"%PDF-1.4")

        with pytest.raises(CarrierValidationError):
            await _adapter(handler).label("1", LabelFormat.ZPL, _account())
        assert called is False

    @pytest.mark.anyio
    async def test_an_error_in_json_is_not_a_form(self) -> None:
        # Ошибка приходит тем же путём, но телом JSON. Отдав её как файл,
        # мы напечатали бы текст ошибки вместо бланка.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "ORDER_NOT_FOUND", "description": "нет"})

        with pytest.raises(CarrierError):
            await _adapter(handler).label("1", LabelFormat.PDF_A4, _account())

    @pytest.mark.anyio
    async def test_an_empty_file_is_not_a_form(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"", headers={"content-type": "application/pdf"})

        with pytest.raises(CarrierError):
            await _adapter(handler).label("1", LabelFormat.PDF_A4, _account())


class TestWhatIsStillMissing:
    """Нереализованное отказывает вслух, а не отдаёт пустоту."""

    def test_the_declared_label_format_matches_the_implementation(self) -> None:
        assert PochtaAdapter.capabilities.supported_label_formats == (LabelFormat.PDF_A4,)

    @pytest.mark.anyio
    async def test_tracking_and_cancel_still_refuse_with_a_reason(self) -> None:
        adapter = PochtaAdapter()
        with pytest.raises(CarrierError):
            await adapter.track("1", _account())
        with pytest.raises(CarrierError):
            await adapter.cancel("1", _account())
