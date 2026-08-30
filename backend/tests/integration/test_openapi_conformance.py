"""Соответствие реализации контракту ``docs/tz/v3/openapi.yaml``.

Контракт — источник истины: по нему фронт генерирует типизированный клиент,
и расхождение кода с ним есть ошибка кода, а не повод поправить контракт.

Тест сравнивает не «всё подряд», а то, что действительно ломает клиента:

* путь, который мы объявили реализованным, обязан существовать в контракте;
* у общих схем обязаны совпадать имена и обязательность полей;
* нереализованные пути перечисляются явно — чтобы список сокращался
  осознанно, а не забывался.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "tz" / "v3" / "openapi.yaml"

#: Наши пути, которых в контракте нет — и это законно: контракт заморожен
#: как P0-набор (``README_START_DEVELOPMENT.txt``), а не как исчерпывающий
#: список. Перечислены явно, чтобы новый путь требовал осознанного решения:
#: не должен ли он быть в контракте.
BEYOND_CONTRACT: frozenset[str] = frozenset(
    {
        # Продление сессии и текущий пользователь: контракт описывает только вход.
        "/v1/auth/me",
        "/v1/auth/refresh",
        "/v1/users",
        # Адресная книга: в ТЗ v3 среди сущностей её нет, решение сохранить —
        # см. docs/status.md, переход на ТЗ v3.
        "/v1/counterparties",
        "/v1/counterparties/{counterparty_id}",
        "/v1/counterparties/{counterparty_id}/addresses",
        "/v1/parties/lookup",
        # Справочники: вход оператора, а не публичный контракт.
        "/v1/addresses/normalize",
        "/v1/cities/suggest",
        "/v1/carriers/{code}/terminals",
        # Ручной разбор сопоставления городов — администрирование платформы.
        "/v1/admin/city-mappings",
        "/v1/admin/city-mappings/{item_id}/confirm",
    }
)

#: Пути контракта, которые ещё не реализованы. Список обязан сокращаться.
#: Пустой список означает, что контракт закрыт целиком.
NOT_IMPLEMENTED_YET: frozenset[str] = frozenset(
    {
        "/v1/routing/quote",
        "/v1/decisions",
        "/v1/shipments",
        "/v1/shipments/{shipment_id}",
        "/v1/shipments/{shipment_id}/tracking",
        "/v1/carriers",
        "/v1/analytics/carriers",
        "/v1/webhooks/{carrier_code}",
    }
)


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


@pytest.fixture
def generated(app: FastAPI) -> dict[str, Any]:
    return app.openapi()


def _required(schema: dict[str, Any], name: str) -> set[str]:
    return set(schema["components"]["schemas"][name].get("required", []))


def _properties(schema: dict[str, Any], name: str) -> set[str]:
    return set(schema["components"]["schemas"][name].get("properties", {}))


class TestPaths:
    def test_every_path_is_either_in_the_contract_or_declared(
        self, contract: dict[str, Any], generated: dict[str, Any]
    ) -> None:
        """Незаявленный путь вне контракта — почти всегда недосмотр.

        Либо его забыли внести в контракт, и тогда фронт о нём не узнает,
        либо он служебный — и это надо сказать вслух, в ``BEYOND_CONTRACT``.
        Служебные пути вне ``/v1`` (проверки живости, схема) сюда не входят:
        они не часть публичного API.
        """
        ours = {p for p in generated["paths"] if p.startswith("/v1/")}
        undeclared = sorted(ours - set(contract["paths"]) - BEYOND_CONTRACT)
        assert not undeclared, f"пути вне контракта и не объявленные: {undeclared}"

    def test_the_beyond_contract_list_is_honest(
        self, contract: dict[str, Any], generated: dict[str, Any]
    ) -> None:
        """Путь, попавший в контракт, не должен числиться служебным."""
        overlap = sorted(BEYOND_CONTRACT & set(contract["paths"]))
        assert not overlap, f"есть в контракте, убрать из BEYOND_CONTRACT: {overlap}"

    def test_the_unimplemented_list_is_honest(
        self, contract: dict[str, Any], generated: dict[str, Any]
    ) -> None:
        """Реализованный путь не должен оставаться в списке нереализованных."""
        ours = {p for p in generated["paths"] if p.startswith("/v1/")}
        stale = sorted(NOT_IMPLEMENTED_YET & ours)
        assert not stale, f"уже реализовано, убрать из NOT_IMPLEMENTED_YET: {stale}"

    def test_nothing_is_forgotten(
        self, contract: dict[str, Any], generated: dict[str, Any]
    ) -> None:
        """Каждый путь контракта либо реализован, либо назван в списке."""
        ours = {p for p in generated["paths"] if p.startswith("/v1/")}
        missing = sorted(set(contract["paths"]) - ours - NOT_IMPLEMENTED_YET)
        assert not missing, f"путь контракта не реализован и не назван: {missing}"


class TestMoneySchema:
    def test_money_is_minor_units_and_a_currency(
        self, contract: dict[str, Any], generated: dict[str, Any]
    ) -> None:
        """ADR-0011: целое число минорных единиц, никакого float."""
        assert _required(contract, "Money") == {"amount_minor", "currency"}
        assert _required(generated, "MoneySchema") == {"amount_minor", "currency"}
        assert (
            generated["components"]["schemas"]["MoneySchema"]["properties"]["amount_minor"]["type"]
            == "integer"
        )


class TestRateRequest:
    def test_required_fields_match(
        self, contract: dict[str, Any], generated: dict[str, Any]
    ) -> None:
        """Обязательное по контракту поле не может быть у нас необязательным.

        Обратное допустимо: мы вправе задать разумное значение по умолчанию
        там, где контракт требует поле явно, — клиент от этого не сломается.
        """
        contract_required = _required(contract, "RateRequest")
        ours = _properties(generated, "RateRequestIn")
        assert contract_required <= ours, f"нет полей: {sorted(contract_required - ours)}"

    def test_address_shape_matches(
        self, contract: dict[str, Any], generated: dict[str, Any]
    ) -> None:
        assert _required(contract, "Address") <= _properties(generated, "AddressSchema")
        assert _properties(contract, "Address") == _properties(generated, "AddressSchema")

    def test_package_is_grams_and_millimetres(
        self, contract: dict[str, Any], generated: dict[str, Any]
    ) -> None:
        """Целые граммы и миллиметры: дробный вес на границе API — та же
        проблема округления, что и дробные деньги."""
        assert _properties(contract, "Package") == _properties(generated, "PackageSchema")
        assert _required(contract, "Package") <= _required(generated, "PackageSchema")


class TestRateResponse:
    def test_required_fields_match(
        self, contract: dict[str, Any], generated: dict[str, Any]
    ) -> None:
        assert _required(contract, "RateResponse") <= _required(generated, "RateResponse")

    def test_offer_fields_match(self, contract: dict[str, Any], generated: dict[str, Any]) -> None:
        """Поле контракта, которого у нас нет, клиент прочитает как undefined."""
        expected = _properties(contract, "RateOffer")
        ours = _properties(generated, "RateOfferOut")
        assert expected <= ours, f"нет полей выдачи: {sorted(expected - ours)}"

    def test_cost_component_types_match(
        self, contract: dict[str, Any], generated: dict[str, Any]
    ) -> None:
        """Расхождение в перечне типов даёт строку стоимости, которую фронт
        не сможет ни назвать, ни сгруппировать."""
        expected = set(
            contract["components"]["schemas"]["CostComponent"]["properties"]["type"]["enum"]
        )
        ours = set(generated["components"]["schemas"]["CostComponentType"]["enum"])
        assert ours == expected

    def test_strategies_match(self, contract: dict[str, Any], generated: dict[str, Any]) -> None:
        expected = set(
            contract["components"]["schemas"]["RoutingRequest"]["properties"]["strategy"]["enum"]
        )
        ours = set(generated["components"]["schemas"]["RoutingStrategy"]["enum"])
        assert ours == expected


class TestAuth:
    def test_login_response_matches(
        self, contract: dict[str, Any], generated: dict[str, Any]
    ) -> None:
        assert _required(contract, "AuthResponse") <= _properties(generated, "TokenPair")
