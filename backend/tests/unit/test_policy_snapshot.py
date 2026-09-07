"""Замороженный вердикт политики: запись и чтение.

Формат — единственный канал между расчётом, который знает факты запроса,
и решением, которое принимается позже и восстановить их не может. Разойдись
запись с чтением — автовыбор молча перестанет срабатывать, а метрика
«решений без человека» останется нулём. Ровно эту поломку задача и чинит,
поэтому у неё есть свой тест.
"""

from __future__ import annotations

from typing import Any

import pytest

from aerogram.routing.rules import Policy, RequestFacts
from aerogram.routing.snapshot import (
    POLICY_SNAPSHOT_VERSION,
    dump_policy_snapshot,
    load_policy_snapshot,
)
from aerogram.shared.enums import CargoType, SelectionRule
from aerogram.shared.ids import uuid7
from aerogram.shared.money import Money

MOSCOW = uuid7()
VLADIVOSTOK = uuid7()
RULE = uuid7()


def facts(**overrides: Any) -> RequestFacts:
    defaults: dict[str, Any] = {
        "origin_fias_id": VLADIVOSTOK,
        "destination_fias_id": MOSCOW,
        "billable_weight_grams": 12_000,
        "cargo_value": Money(48_000_000, "RUB"),
        "cargo_type": CargoType.EQUIPMENT,
        "dangerous": True,
    }
    return RequestFacts(**{**defaults, **overrides})


def policy(**overrides: Any) -> Policy:
    defaults: dict[str, Any] = {
        "verdicts": (),
        "require_insurance": True,
        "insurance_rule": "дорогое страхуем",
        "auto_select": SelectionRule.CHEAPEST,
        "auto_select_rule": "берём дешёвое",
        "auto_select_rule_id": RULE,
    }
    return Policy(**{**defaults, **overrides})


class TestRoundTrip:
    def test_facts_survive_unchanged(self) -> None:
        """Включая валюту: сумма без валюты не существует (CLAUDE.md §6)."""
        loaded = load_policy_snapshot(dump_policy_snapshot(facts(), policy()))
        assert loaded is not None
        assert loaded.facts == facts()
        assert loaded.facts.cargo_value.currency == "RUB"

    def test_the_rule_survives_by_id_and_by_name(self) -> None:
        """Имя без идентификатора не переживает переименование правила."""
        loaded = load_policy_snapshot(dump_policy_snapshot(facts(), policy()))
        assert loaded is not None
        assert loaded.auto_select_rule_id == RULE
        assert loaded.auto_select_rule == "берём дешёвое"
        assert loaded.auto_select is SelectionRule.CHEAPEST

    def test_dangerous_false_is_not_lost(self) -> None:
        """``False`` — значение признака, а не его отсутствие."""
        loaded = load_policy_snapshot(dump_policy_snapshot(facts(dangerous=False), policy()))
        assert loaded is not None
        assert loaded.facts.dangerous is False

    def test_unresolved_cities_survive_as_none(self) -> None:
        """«Город не разрешён» — законное состояние, и оно значимо.

        Оно не совпадает ни с каким перечислением в условии по направлению,
        и потерять его значило бы применить правило, которое при расчёте
        не совпадало.
        """
        loaded = load_policy_snapshot(
            dump_policy_snapshot(facts(origin_fias_id=None, destination_fias_id=None), policy())
        )
        assert loaded is not None
        assert loaded.facts.origin_fias_id is None
        assert loaded.facts.destination_fias_id is None

    def test_a_policy_without_auto_select_survives(self) -> None:
        loaded = load_policy_snapshot(
            dump_policy_snapshot(
                facts(),
                policy(auto_select=None, auto_select_rule=None, auto_select_rule_id=None),
            )
        )
        assert loaded is not None
        assert loaded.auto_select is None

    def test_the_snapshot_is_json_serialisable(self) -> None:
        """Колонка JSONB: UUID и Decimal туда не кладутся."""
        import json

        json.dumps(dump_policy_snapshot(facts(), policy()), ensure_ascii=False)


class TestRefusalToGuess:
    def test_no_snapshot_at_all(self) -> None:
        """Расчёт снят до миграции. Честный ответ — «неизвестно»."""
        assert load_policy_snapshot(None) is None
        assert load_policy_snapshot({}) is None

    def test_another_format_version(self) -> None:
        """Разбор наугад назвал бы правило по половине фактов."""
        data = dump_policy_snapshot(facts(), policy())
        data["version"] = POLICY_SNAPSHOT_VERSION + 1
        assert load_policy_snapshot(data) is None

    @pytest.mark.parametrize(
        "corrupt",
        [
            {"version": POLICY_SNAPSHOT_VERSION},
            {"version": POLICY_SNAPSHOT_VERSION, "facts": {}},
            {"version": POLICY_SNAPSHOT_VERSION, "facts": None},
        ],
    )
    def test_a_broken_body_is_not_guessed(self, corrupt: dict[str, Any]) -> None:
        assert load_policy_snapshot(corrupt) is None

    def test_an_unknown_cargo_type_is_refused(self) -> None:
        """Словарь мог смениться; принять чужое значение молча нельзя."""
        data = dump_policy_snapshot(facts(), policy())
        data["facts"]["cargo_type"] = "неведомое"
        assert load_policy_snapshot(data) is None

    def test_an_unknown_selection_rule_is_refused(self) -> None:
        data = dump_policy_snapshot(facts(), policy())
        data["auto_select"] = "greenest"
        assert load_policy_snapshot(data) is None

    def test_a_money_amount_without_currency_is_refused(self) -> None:
        data = dump_policy_snapshot(facts(), policy())
        del data["facts"]["cargo_value"]["currency"]
        assert load_policy_snapshot(data) is None
