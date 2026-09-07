"""Замороженный вердикт политики: что расчёт передаёт решению.

Правила применяются при расчёте, решение принимается позже. Восстановить
свойства запроса в ``routing`` нельзя: двух полей из шести — разрешённых
идентификаторов городов — нет в снимке запроса по контракту, а повторное
разрешение города не чистая функция. Оно уходит к ДаData и **записывает**
найденный город в общую таблицу без RLS; город, появившийся между расчётом
и рекомендацией, включил бы правило с условием по направлению, которого
при расчёте не было (ADR-0029).

Поэтому вердикт вычисляется один раз там, где факты уже есть, и приезжает
сюда данными. Здесь только формат: ни базы, ни сети, ни правил.

**Персональных данных в снимке нет по построению** — идентификаторы ФИАС,
граммы, сумма с валютой, тип груза, признак опасности. Улица и дом остаются
в снимке запроса и сюда не попадают, поэтому маскировать нечего
(CLAUDE.md §6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aerogram.routing.rules import Policy, RequestFacts
from aerogram.shared.enums import CargoType, SelectionRule
from aerogram.shared.logging import get_logger
from aerogram.shared.money import Money

__all__ = [
    "POLICY_SNAPSHOT_VERSION",
    "PolicySnapshot",
    "dump_policy_snapshot",
    "load_policy_snapshot",
]

log = get_logger(__name__)

#: Версия формата. Растёт при ЛЮБОМ несовместимом изменении состава полей.
#: Снимок чужой версии не разбирается наугад: автовыбор по половине фактов
#: назвал бы правило, которое при расчёте не совпадало.
POLICY_SNAPSHOT_VERSION = 1


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """Вердикт политики, каким он был в момент расчёта."""

    facts: RequestFacts
    auto_select: SelectionRule | None
    auto_select_rule_id: UUID | None
    auto_select_rule: str | None
    require_insurance: bool
    insurance_rule: str | None


def dump_policy_snapshot(facts: RequestFacts, policy: Policy) -> dict[str, Any]:
    """Вердикт → JSONB строки расчёта."""
    return {
        "version": POLICY_SNAPSHOT_VERSION,
        "facts": {
            # ``str`` у UUID и явные ключи вместо ``asdict``: снимок читают
            # через год, и переименование поля дата-класса не должно молча
            # разойтись с тем, что уже лежит в базе.
            "origin_fias_id": str(facts.origin_fias_id) if facts.origin_fias_id else None,
            "destination_fias_id": (
                str(facts.destination_fias_id) if facts.destination_fias_id else None
            ),
            "billable_weight_grams": facts.billable_weight_grams,
            "cargo_value": {
                "amount_minor": facts.cargo_value.amount_minor,
                "currency": facts.cargo_value.currency,
            },
            "cargo_type": facts.cargo_type.value,
            "dangerous": facts.dangerous,
        },
        "auto_select": policy.auto_select.value if policy.auto_select else None,
        "auto_select_rule_id": (
            str(policy.auto_select_rule_id) if policy.auto_select_rule_id else None
        ),
        "auto_select_rule": policy.auto_select_rule,
        "require_insurance": policy.require_insurance,
        "insurance_rule": policy.insurance_rule,
    }


def load_policy_snapshot(data: dict[str, Any] | None) -> PolicySnapshot | None:
    """JSONB → вердикт, либо ``None``.

    ``None`` — честный ответ «неизвестно» на три разных случая: расчёт снят
    до появления колонки, снимок чужой версии, снимок не разбирается.
    Во всех трёх автовыбор не срабатывает, и это правильно: угадывать вердикт
    по историческому снимку значит объяснить решение правилом, которое
    к нему могло не иметь отношения.
    """
    if not data:
        return None
    if data.get("version") != POLICY_SNAPSHOT_VERSION:
        log.warning("routing.policy_snapshot_version", version=data.get("version"))
        return None
    try:
        raw = data["facts"]
        value = raw["cargo_value"]
        facts = RequestFacts(
            origin_fias_id=_uuid(raw["origin_fias_id"]),
            destination_fias_id=_uuid(raw["destination_fias_id"]),
            billable_weight_grams=int(raw["billable_weight_grams"]),
            cargo_value=Money(int(value["amount_minor"]), str(value["currency"])),
            cargo_type=CargoType(raw["cargo_type"]),
            dangerous=bool(raw["dangerous"]),
        )
        rule = data["auto_select"]
        return PolicySnapshot(
            facts=facts,
            auto_select=SelectionRule(rule) if rule else None,
            auto_select_rule_id=_uuid(data["auto_select_rule_id"]),
            auto_select_rule=data["auto_select_rule"],
            require_insurance=bool(data["require_insurance"]),
            insurance_rule=data["insurance_rule"],
        )
    except (KeyError, TypeError, ValueError) as error:
        # Ловим широко намеренно: снимок мог быть записан старым кодом,
        # правкой в базе или сломанной миграцией. Разница между видами
        # поломки здесь не важна — важно не принять решение по обломкам.
        log.warning("routing.policy_snapshot_unreadable", error_type=type(error).__name__)
        return None


def _uuid(value: object) -> UUID | None:
    return UUID(str(value)) if value else None
