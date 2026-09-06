"""Язык правил маршрутизации: разбор, сопоставление, разрешение противоречий.

Тесты здесь проверяют не «работает ли функция», а то, что правило срабатывает
именно на том, на чём человек его писал. Ошибка в этом коде не падает: она
молча меняет состав выдачи, и обнаруживается счётом от перевозчика, которого
в выдаче быть не должно было.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from aerogram.routing.rules import (
    EMPTY_POLICY_VERSION,
    ParsedRule,
    RequestFacts,
    RuleActions,
    RuleConditions,
    evaluate,
    parse_rules,
    policy_fingerprint,
)
from aerogram.shared.enums import CargoType, IneligibilityReason, SelectionRule
from aerogram.shared.errors import BrokenRoutingRule
from aerogram.shared.ids import uuid7
from aerogram.shared.money import Money

MOSCOW = UUID("0c5b2444-70a0-4932-980c-b4dc0d3f02b5")
KALININGRAD = UUID("90ac8bda-8b0b-4c7a-9dd5-b6e2b3c4d5e6")
VLADIVOSTOK = UUID("43909681-d6e1-432d-b61f-ddac393cb5da")

CARRIERS = ["cdek", "dellin", "pecom", "pochta"]


def facts(
    *,
    origin: UUID | None = MOSCOW,
    destination: UUID | None = VLADIVOSTOK,
    grams: int = 5_000,
    value: Money = Money(1_000_00, "RUB"),
    cargo_type: CargoType = CargoType.PARCEL,
    dangerous: bool = False,
) -> RequestFacts:
    return RequestFacts(
        origin_fias_id=origin,
        destination_fias_id=destination,
        billable_weight_grams=grams,
        cargo_value=value,
        cargo_type=cargo_type,
        dangerous=dangerous,
    )


def rule(
    conditions: dict[str, Any],
    actions: dict[str, Any],
    *,
    name: str = "правило",
    priority: int = 10,
) -> ParsedRule:
    """Разобранное правило из того же JSON, что лежал бы в базе."""
    return ParsedRule(
        id=uuid7(),
        name=name,
        priority=priority,
        conditions=RuleConditions.model_validate(conditions),
        actions=RuleActions.model_validate(actions),
    )


class _Row:
    """Строка ``routing_rules`` без SQLAlchemy — ровно то, что нужно разбору."""

    def __init__(
        self,
        conditions: dict[str, Any],
        actions: dict[str, Any],
        *,
        name: str = "правило",
        priority: int = 10,
    ) -> None:
        self.id = uuid7()
        self.name = name
        self.priority = priority
        self.conditions = conditions
        self.actions = actions


class TestConditionsAreValidatedOnWrite:
    """Состав условия проверяется при записи, а не при применении."""

    def test_an_unknown_key_is_refused(self) -> None:
        """Опечатка не должна превращать правило в «совпадает со всем».

        ``{"carier": ["cdek"]}`` при разборе по известным ключам дал бы
        пустое условие — то есть запрет на всех перевозчиков сразу.
        """
        with pytest.raises(ValidationError) as error:
            RuleConditions.model_validate({"carier": ["cdek"]})
        assert "carier" in str(error.value)

    def test_an_unknown_key_inside_a_nested_condition_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RuleConditions.model_validate({"weight": {"min_kg": 30}})

    def test_an_empty_condition_matches_everything(self) -> None:
        """Пустое условие законно: так пишется «страховать всё»."""
        conditions = RuleConditions.model_validate({})
        assert conditions.carrier is None

    def test_carrier_codes_are_normalised(self) -> None:
        conditions = RuleConditions.model_validate({"carrier": [" CDEK ", "cdek", "Pecom"]})
        assert conditions.carrier == ["cdek", "pecom"]

    def test_an_empty_carrier_list_is_refused(self) -> None:
        """Пустой список читался бы как «ни один», а написан как «эти»."""
        with pytest.raises(ValidationError):
            RuleConditions.model_validate({"carrier": []})

    def test_a_direction_without_sides_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RuleConditions.model_validate({"direction": {}})

    def test_weight_bounds_must_not_be_inverted(self) -> None:
        with pytest.raises(ValidationError) as error:
            RuleConditions.model_validate({"weight": {"min_grams": 30_000, "max_grams": 1_000}})
        assert "больше верхней" in str(error.value)

    def test_cargo_value_requires_a_currency(self) -> None:
        """Сумма без валюты не существует (CLAUDE.md §6)."""
        with pytest.raises(ValidationError):
            RuleConditions.model_validate({"cargo_value": {"min_minor": 50_000_000}})

    def test_cargo_value_currency_is_upper_cased(self) -> None:
        conditions = RuleConditions.model_validate(
            {"cargo_value": {"min_minor": 1, "currency": "rub"}}
        )
        assert conditions.cargo_value is not None
        assert conditions.cargo_value.currency == "RUB"

    def test_a_three_digit_currency_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RuleConditions.model_validate({"cargo_value": {"min_minor": 1, "currency": "643"}})


class TestActionsAreValidatedOnWrite:
    def test_a_rule_without_an_action_is_refused(self) -> None:
        with pytest.raises(ValidationError) as error:
            RuleActions.model_validate({})
        assert "без действия" in str(error.value)

    def test_two_actions_in_one_rule_are_refused(self) -> None:
        """У правила одно имя и одна строка объяснения — значит и одно дело."""
        with pytest.raises(ValidationError) as error:
            RuleActions.model_validate({"deny": True, "require_insurance": True})
        assert "одно действие" in str(error.value)

    def test_a_false_boolean_action_is_refused(self) -> None:
        """``{"deny": false}`` — выключенное правило, притворяющееся включённым."""
        with pytest.raises(ValidationError):
            RuleActions.model_validate({"deny": False})

    def test_an_unknown_selection_rule_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RuleActions.model_validate({"auto_select": "whatever"})

    def test_a_carrier_condition_does_not_fit_insurance(self) -> None:
        """Страхование и автовыбор — решения по всей отправке.

        Условие ``carrier`` рядом с ними выглядело бы работающим и не значило
        бы ничего, поэтому оно отвергается при записи.
        """
        rows = [_Row({"carrier": ["pochta"]}, {"require_insurance": True}, name="страховать")]
        with pytest.raises(BrokenRoutingRule):
            parse_rules(list(rows))

    def test_a_carrier_condition_fits_deny(self) -> None:
        parsed = parse_rules([_Row({"carrier": ["pochta"]}, {"deny": True})])
        assert parsed[0].conditions.carrier == ["pochta"]


class TestParsing:
    def test_a_broken_rule_stops_the_whole_set(self) -> None:
        """Пропустить нечитаемое правило значит применить политику не полностью.

        И именно в ту сторону, в которую ошибаться нельзя: пропущенный запрет
        открывает то, что было закрыто.
        """
        rows = [
            _Row({}, {"deny": True}, name="хорошее"),
            _Row({"carier": ["cdek"]}, {"deny": True}, name="сломанное"),
        ]
        with pytest.raises(BrokenRoutingRule) as error:
            parse_rules(list(rows))
        assert "сломанное" in error.value.message_ru

    def test_rules_are_sorted_by_priority(self) -> None:
        """Порядок задаётся разбором, а не запросом.

        Молчаливая зависимость от порядка строк однажды поменяла бы исход
        при изменении запроса, который к правилам отношения не имеет.
        """
        rows = [
            _Row({}, {"deny": True}, name="третье", priority=30),
            _Row({}, {"deny": True}, name="первое", priority=10),
            _Row({}, {"deny": True}, name="второе", priority=20),
        ]
        assert [r.name for r in parse_rules(list(rows))] == ["первое", "второе", "третье"]


class TestDeny:
    def test_a_carrier_only_condition_reads_as_a_blacklist(self) -> None:
        policy = evaluate([rule({"carrier": ["pochta"]}, {"deny": True})], facts(), CARRIERS)
        verdict = policy.verdict("pochta")
        assert verdict is not None
        assert not verdict.allowed
        assert verdict.reason is IneligibilityReason.CARRIER_BLACKLISTED
        assert policy.allowed_codes == ("cdek", "dellin", "pecom")

    def test_a_cargo_condition_reads_as_a_cargo_restriction(self) -> None:
        """«Этому перевозчику нельзя такой груз» — не то же, что чёрный список."""
        policy = evaluate(
            [rule({"carrier": ["pochta"], "dangerous": True}, {"deny": True})],
            facts(dangerous=True),
            CARRIERS,
        )
        verdict = policy.verdict("pochta")
        assert verdict is not None
        assert verdict.reason is IneligibilityReason.CARGO_RESTRICTED

    def test_a_direction_condition_reads_as_tenant_policy(self) -> None:
        policy = evaluate(
            [rule({"carrier": ["cdek"], "direction": {"to": [str(VLADIVOSTOK)]}}, {"deny": True})],
            facts(),
            CARRIERS,
        )
        verdict = policy.verdict("cdek")
        assert verdict is not None
        assert verdict.reason is IneligibilityReason.TENANT_POLICY

    def test_a_denied_carrier_keeps_its_row_and_the_rule_name(self) -> None:
        """Запрещённое не исчезает из выдачи: оператор видит причину."""
        policy = evaluate(
            [rule({"carrier": ["pochta"]}, {"deny": True}, name="Почтой не возим")],
            facts(),
            CARRIERS,
        )
        assert [v.carrier_code for v in policy.blocked] == ["pochta"]
        assert policy.blocked[0].rule_name == "Почтой не возим"

    def test_a_rule_that_does_not_match_the_request_denies_nothing(self) -> None:
        policy = evaluate(
            [rule({"carrier": ["pochta"], "weight": {"min_grams": 30_000}}, {"deny": True})],
            facts(grams=5_000),
            CARRIERS,
        )
        assert policy.allowed_codes == tuple(CARRIERS)

    def test_a_condition_without_carrier_denies_everyone(self) -> None:
        """«Опасные грузы не возим вовсе» — законная и страшная запись."""
        policy = evaluate(
            [rule({"dangerous": True}, {"deny": True})], facts(dangerous=True), CARRIERS
        )
        assert policy.allowed_codes == ()


class TestWhitelist:
    def test_a_whitelist_forbids_everyone_it_does_not_name(self) -> None:
        """Whitelist — это не «разрешить вот это», а «кроме этого — ничего»."""
        policy = evaluate(
            [
                rule(
                    {"carrier": ["dellin", "pecom"], "weight": {"min_grams": 30_000}},
                    {"allow": True},
                )
            ],
            facts(grams=50_000),
            CARRIERS,
        )
        assert policy.allowed_codes == ("dellin", "pecom")
        verdict = policy.verdict("cdek")
        assert verdict is not None
        assert verdict.reason is IneligibilityReason.NOT_IN_WHITELIST

    def test_a_whitelist_that_did_not_match_the_request_does_nothing(self) -> None:
        """Иначе «в Калининград — только Почтой» запрещало бы всё и везде."""
        policy = evaluate(
            [
                rule(
                    {"carrier": ["pochta"], "direction": {"to": [str(KALININGRAD)]}},
                    {"allow": True},
                )
            ],
            facts(destination=VLADIVOSTOK),
            CARRIERS,
        )
        assert policy.allowed_codes == tuple(CARRIERS)

    def test_deny_beats_allow(self) -> None:
        """Даже когда запрет слабее по приоритету.

        Ошибка в сторону запрета стоит одной ручной отправки, ошибка в другую
        сторону — нарушения политики, о котором никто не узнает.
        """
        policy = evaluate(
            [
                rule({"carrier": ["cdek"]}, {"deny": True}, name="запрет", priority=1),
                rule({"carrier": ["cdek", "pecom"]}, {"allow": True}, name="белый", priority=99),
            ],
            facts(),
            CARRIERS,
        )
        verdict = policy.verdict("cdek")
        assert verdict is not None
        assert not verdict.allowed
        assert verdict.rule_name == "запрет"
        assert policy.allowed_codes == ("pecom",)

    def test_two_whitelists_narrow_each_other(self) -> None:
        """Оба белых списка действуют: разрешён тот, кого назвали оба."""
        policy = evaluate(
            [
                rule({"carrier": ["cdek", "pecom"]}, {"allow": True}, priority=1),
                rule({"carrier": ["pecom", "dellin"]}, {"allow": True}, priority=2),
            ],
            facts(),
            CARRIERS,
        )
        assert policy.allowed_codes == ("pecom",)


class TestMatching:
    def test_weight_bounds_are_inclusive(self) -> None:
        deny = [rule({"weight": {"min_grams": 30_000}}, {"deny": True})]
        assert evaluate(deny, facts(grams=29_999), ["cdek"]).allowed_codes == ("cdek",)
        assert evaluate(deny, facts(grams=30_000), ["cdek"]).allowed_codes == ()

    def test_value_bounds_are_inclusive(self) -> None:
        deny = [rule({"cargo_value": {"min_minor": 500_000, "currency": "RUB"}}, {"deny": True})]
        assert evaluate(deny, facts(value=Money(499_999, "RUB")), ["cdek"]).allowed_codes == (
            "cdek",
        )
        assert evaluate(deny, facts(value=Money(500_000, "RUB")), ["cdek"]).allowed_codes == ()

    def test_a_different_currency_does_not_match_instead_of_comparing(self) -> None:
        """«50 000» в рублях и в тенге — разные величины (CLAUDE.md §6)."""
        deny = [rule({"cargo_value": {"min_minor": 500_000, "currency": "RUB"}}, {"deny": True})]
        assert evaluate(deny, facts(value=Money(900_000, "KZT")), ["cdek"]).allowed_codes == (
            "cdek",
        )

    def test_an_unresolved_city_does_not_match_a_direction(self) -> None:
        """Правило про Калининград не применяется к запросу «неизвестно куда»."""
        deny = [rule({"direction": {"to": [str(KALININGRAD)]}}, {"deny": True})]
        assert evaluate(deny, facts(destination=None), ["cdek"]).allowed_codes == ("cdek",)

    def test_a_direction_with_one_side_leaves_the_other_free(self) -> None:
        deny = [rule({"direction": {"to": [str(VLADIVOSTOK)]}}, {"deny": True})]
        assert evaluate(deny, facts(origin=KALININGRAD), ["cdek"]).allowed_codes == ()

    def test_keys_inside_a_condition_are_joined_by_and(self) -> None:
        """Совпало одно из двух — правило не срабатывает."""
        deny = [
            rule(
                {"weight": {"min_grams": 30_000}, "cargo_type": ["cargo"]},
                {"deny": True},
            )
        ]
        assert evaluate(
            deny, facts(grams=50_000, cargo_type=CargoType.PARCEL), ["cdek"]
        ).allowed_codes == ("cdek",)
        assert (
            evaluate(deny, facts(grams=50_000, cargo_type=CargoType.CARGO), ["cdek"]).allowed_codes
            == ()
        )

    def test_dangerous_false_is_a_condition_and_not_an_absence(self) -> None:
        """``{"dangerous": false}`` — правило про неопасные грузы, а не про все."""
        deny = [rule({"dangerous": False}, {"deny": True})]
        assert evaluate(deny, facts(dangerous=False), ["cdek"]).allowed_codes == ()
        assert evaluate(deny, facts(dangerous=True), ["cdek"]).allowed_codes == ("cdek",)


class TestInsurance:
    def test_one_matching_rule_makes_insurance_mandatory(self) -> None:
        policy = evaluate(
            [
                rule(
                    {"cargo_value": {"min_minor": 500_000, "currency": "RUB"}},
                    {"require_insurance": True},
                    name="дорогое страхуем",
                )
            ],
            facts(value=Money(900_000, "RUB")),
            CARRIERS,
        )
        assert policy.require_insurance
        assert policy.insurance_rule == "дорогое страхуем"

    def test_below_the_threshold_insurance_is_not_forced(self) -> None:
        policy = evaluate(
            [
                rule(
                    {"cargo_value": {"min_minor": 500_000, "currency": "RUB"}},
                    {"require_insurance": True},
                )
            ],
            facts(value=Money(100_000, "RUB")),
            CARRIERS,
        )
        assert not policy.require_insurance
        assert policy.insurance_rule is None

    def test_strict_beats_lax(self) -> None:
        """Требование страхования побеждает его отсутствие независимо от приоритета."""
        policy = evaluate(
            [
                rule({}, {"auto_select": "cheapest"}, priority=99),
                rule({"cargo_type": ["equipment"]}, {"require_insurance": True}, priority=1),
            ],
            facts(cargo_type=CargoType.EQUIPMENT),
            CARRIERS,
        )
        assert policy.require_insurance


class TestAutoSelect:
    def test_the_highest_priority_rule_wins(self) -> None:
        """У ``cheapest`` и ``fastest`` нет строгого: решает порядок, заданный человеком."""
        policy = evaluate(
            [
                rule({}, {"auto_select": "cheapest"}, name="дешевле", priority=10),
                rule({}, {"auto_select": "fastest"}, name="быстрее", priority=20),
            ],
            facts(),
            CARRIERS,
        )
        assert policy.auto_select is SelectionRule.FASTEST
        assert policy.auto_select_rule == "быстрее"

    def test_without_a_matching_rule_there_is_no_auto_select(self) -> None:
        policy = evaluate(
            [rule({"cargo_type": ["documents"]}, {"auto_select": "cheapest"})],
            facts(cargo_type=CargoType.CARGO),
            CARRIERS,
        )
        assert policy.auto_select is None


class TestFingerprint:
    def test_changing_any_rule_changes_the_version(self) -> None:
        """Дефект, ради которого отпечаток и заведён (ADR-0028, §6).

        Версия бралась от правила с наибольшим приоритетом, и изменение
        любого другого правила её не меняло: два разных набора давали
        одинаковую версию.
        """
        low = rule({"carrier": ["cdek"]}, {"deny": True}, priority=1)
        high = rule({}, {"auto_select": "cheapest"}, priority=99)
        before = policy_fingerprint([low, high])

        changed = ParsedRule(
            id=low.id,
            name=low.name,
            priority=low.priority,
            conditions=RuleConditions.model_validate({"carrier": ["pochta"]}),
            actions=low.actions,
        )
        assert policy_fingerprint([changed, high]) != before

    def test_the_same_set_gives_the_same_version_in_any_order(self) -> None:
        """Иначе одинаковые политики выглядят в аналитике разными."""
        first = rule({"carrier": ["cdek"]}, {"deny": True}, priority=1)
        second = rule({}, {"auto_select": "cheapest"}, priority=99)
        assert policy_fingerprint([first, second]) == policy_fingerprint([second, first])

    def test_the_name_does_not_change_the_version(self) -> None:
        """Переименование правила не меняет политику, а только подпись к ней."""
        original = rule({"carrier": ["cdek"]}, {"deny": True}, name="было")
        renamed = ParsedRule(
            id=original.id,
            name="стало",
            priority=original.priority,
            conditions=original.conditions,
            actions=original.actions,
        )
        assert policy_fingerprint([original]) == policy_fingerprint([renamed])

    def test_an_empty_set_keeps_the_name_it_always_had(self) -> None:
        """Отсутствие правил — тоже политика, и она обязана быть названа.

        Имя у неё то же, что до ADR-0028: у пустого набора не должно
        появиться второе имя, иначе одна и та же политика выглядит
        в истории двумя разными.
        """
        assert policy_fingerprint([]) == EMPTY_POLICY_VERSION

    def test_a_set_with_rules_is_not_the_empty_version(self) -> None:
        assert policy_fingerprint([rule({}, {"deny": True})]).startswith("policy-")

    def test_the_version_fits_the_column(self) -> None:
        """``policy_version`` — ``String(40)``: длиннее просто не сохранится."""
        assert len(policy_fingerprint([rule({}, {"deny": True})])) <= 40
