"""Разбор входящего вебхука СДЭК.

Фикстуры синтетические и на ступень ниже остальных: официальный SDK покрывает
только подписку на вебхуки, форма тела взята из типов стороннего клиента —
см. tests/fixtures/cdek/README.md.

Главная проверка файла — что статус берётся из ``attributes.code``, а не
из ``attributes.status_code``. Обе колонки есть в теле, обе выглядят статусом,
и ошибка не даёт исключения: нормализатор пометит событие несопоставленным
и поставит ``IN_TRANSIT``. Лента наполнится, доставка не отметится никогда,
и обнаружится это на разборе просрочек.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aerogram.carriers.cdek.adapter import CdekAdapter
from aerogram.carriers.cdek.webhook import parse_order_status
from aerogram.carriers.status_map import normalize_status
from aerogram.shared.enums import ShipmentStatus
from aerogram.shared.errors import CarrierNotConfigured

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cdek"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


@pytest.fixture
def order_status() -> dict[str, Any]:
    return load("webhook_order_status")


class TestOrderStatus:
    def test_the_order_is_identified_by_the_entity_uuid(self, order_status: dict[str, Any]) -> None:
        """Именно этот идентификатор мы храним как ``external_id``.

        ``cdek_number`` для поиска не годится: у возвратного заказа он свой,
        и события возврата приклеились бы к прямому отправлению.
        """
        updates = parse_order_status(order_status)

        assert [u.external_id for u in updates] == ["72753031-2801-4186-a091-0be58cedfee7"]

    def test_the_status_comes_from_code_not_status_code(self, order_status: dict[str, Any]) -> None:
        """В теле лежат оба поля, и легаси-число статусом не является."""
        assert order_status["attributes"]["status_code"] == "3"

        event = parse_order_status(order_status)[0].events[0]

        assert event.status_raw == "RECEIVED_AT_SHIPMENT_WAREHOUSE"

    def test_the_status_is_one_our_map_knows(self, order_status: dict[str, Any]) -> None:
        """Проверка того же с другой стороны: значение обязано сопоставиться.

        Возьми разбор ``status_code``, и здесь было бы ``("IN_TRANSIT", True)``
        — «не сопоставлено», то есть молчаливая потеря статуса.
        """
        event = parse_order_status(order_status)[0].events[0]

        status, unmapped = normalize_status("cdek", event.status_raw)

        assert status is ShipmentStatus.ACCEPTED
        assert unmapped is False

    def test_the_moment_is_utc_from_the_status_time(self, order_status: dict[str, Any]) -> None:
        """Смещение приходит без двоеточия (``+0700``) и обязано разбираться."""
        event = parse_order_status(order_status)[0].events[0]

        assert event.occurred_at == datetime(2026, 9, 3, 7, 21, 5, tzinfo=UTC)

    def test_the_city_and_the_raw_attributes_are_kept(self, order_status: dict[str, Any]) -> None:
        """Сырые атрибуты нужны разбору: причина отказа живёт только в них."""
        event = parse_order_status(order_status)[0].events[0]

        assert event.city == "Новосибирск"
        assert event.raw["cdek_number"] == "1106321645"


class TestWhatIsIgnored:
    def test_a_print_form_event_carries_no_tracking(self) -> None:
        """Готовность печатной формы к ленте отношения не имеет."""
        assert parse_order_status(load("webhook_print_form")) == []

    def test_an_unknown_event_type_is_accepted_silently(self) -> None:
        """Отказ заставил бы перевозчика повторять доставку ненужного события."""
        assert parse_order_status({"type": "PREALERT_CLOSED", "uuid": "x"}) == []

    def test_a_body_without_an_order_id_yields_nothing(self, order_status: dict[str, Any]) -> None:
        """Без идентификатора заказа отправление не найти — записывать нечего."""
        assert parse_order_status({**order_status, "uuid": ""}) == []

    @pytest.mark.parametrize("attributes", [None, "", [], 42])
    def test_broken_attributes_do_not_raise(
        self, order_status: dict[str, Any], attributes: object
    ) -> None:
        """Тело приходит из сети: разбор не имеет права падать исключением."""
        assert parse_order_status({**order_status, "attributes": attributes}) == []


class TestPartialBodies:
    def test_a_status_without_a_time_is_not_invented(self, order_status: dict[str, Any]) -> None:
        """Время события — основа порядка ленты; выдумать его нельзя.

        Заказ при этом назван: принимающая сторона увидит, что событие было,
        но записать в ленту нечего.
        """
        body = {**order_status, "date_time": ""}
        body["attributes"] = {**order_status["attributes"], "status_date_time": ""}

        updates = parse_order_status(body)

        assert [u.external_id for u in updates] == [order_status["uuid"]]
        assert updates[0].events == ()

    def test_the_top_level_time_is_the_fallback(self, order_status: dict[str, Any]) -> None:
        body = {**order_status, "date_time": "2026-09-03T14:21:07+0700"}
        body["attributes"] = {**order_status["attributes"], "status_date_time": None}

        event = parse_order_status(body)[0].events[0]

        assert event.occurred_at == datetime(2026, 9, 3, 7, 21, 7, tzinfo=UTC)

    def test_an_unparsable_time_does_not_raise(self, order_status: dict[str, Any]) -> None:
        body = {**order_status, "date_time": "вчера"}
        body["attributes"] = {**order_status["attributes"], "status_date_time": "позавчера"}

        assert parse_order_status(body)[0].events == ()


class TestSignature:
    def test_verification_still_refuses_and_says_why(self) -> None:
        """Вернуть True значило бы принимать событие от кого угодно.

        По нему меняется статус отправления и считается соблюдение срока,
        поэтому падаем закрыто, пока способ подтверждения не выбран.
        """
        with pytest.raises(CarrierNotConfigured) as refusal:
            CdekAdapter().verify_webhook(b"{}", {}, "secret")

        # Именно «не настроено», а не «перевозчик вернул ошибку»: перевозчик
        # ничего не возвращал, и повторять отказ бесполезно.
        assert "не подписывает" in str(refusal.value)
