"""Язык правил маршрутизации: разбор, сопоставление, разрешение противоречий.

Чистые функции без базы и без сети — как ``intelligence/score.py``, и по той
же причине: это код, где ошибка не падает, а тихо меняет советы. Правило,
которое не сработало, обнаруживается не в тесте и не в логе, а в счёте
от перевозчика, которого не должно было быть в выдаче.

Состав языка и порядок разрешения противоречий — ADR-0028. Здесь он записан
машинно; отклонения от документа в коде считаются ошибкой кода.

Три вещи, которые здесь важнее удобства:

* **неизвестный ключ отвергается** при записи. ``{"carier": ["cdek"]}``
  при разборе «по известным ключам» дало бы пустое условие, то есть правило,
  совпадающее со всем;
* **приоритет больше — правило сильнее.** Направление шкалы выбрано не здесь:
  так его уже читает ``RecommendationService``, и две противоположные
  трактовки одного числа хуже любой из них;
* **ошибка закрывает, а не открывает.** Запрет побеждает разрешение,
  требование страхования побеждает его отсутствие, нечитаемое правило
  останавливает расчёт.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aerogram.shared.enums import CargoType, IneligibilityReason, SelectionRule
from aerogram.shared.errors import BrokenRoutingRule
from aerogram.shared.money import Money

__all__ = [
    "EMPTY_POLICY_VERSION",
    "CargoValueCondition",
    "CarrierVerdict",
    "DirectionCondition",
    "ParsedRule",
    "Policy",
    "RequestFacts",
    "RuleActions",
    "RuleBody",
    "RuleConditions",
    "WeightCondition",
    "evaluate",
    "parse_rules",
    "policy_fingerprint",
]


class _Strict(BaseModel):
    """Общая настройка: неизвестный ключ — отказ, а не молчание."""

    model_config = ConfigDict(extra="forbid")


class DirectionCondition(_Strict):
    """Откуда и куда, идентификаторами ФИАС.

    Отсутствие ``from`` или ``to`` означает «любой»: ``{"to": [...]}`` — это
    правило про всё, что едет в названные города.

    Регионов здесь нет намеренно (ADR-0028): регион в адресе контракта
    необязателен и в снимках обычно пуст, а правило, собранное из пустого
    поля, молча не совпало бы ни с чем.
    """

    from_: list[UUID] | None = Field(default=None, alias="from", min_length=1)
    to: list[UUID] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _at_least_one_side(self) -> DirectionCondition:
        if self.from_ is None and self.to is None:
            raise ValueError("укажите хотя бы одну сторону направления: from или to")
        return self


class WeightCondition(_Strict):
    """Границы расчётного веса всего отправления, включительно.

    Вес именно расчётный, с учётом объёмного (FR-1.2): платит клиент по нему,
    и правило «тяжелее 30 кг — только транспортными компаниями» обязано
    срабатывать на том же числе, которое попадёт в счёт. По фактическому весу
    лёгкая, но объёмная коробка прошла бы мимо правила — ровно тот случай,
    ради которого правило и заводят.
    """

    min_grams: int | None = Field(default=None, ge=0)
    max_grams: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _bounds_make_sense(self) -> WeightCondition:
        if self.min_grams is None and self.max_grams is None:
            raise ValueError("укажите хотя бы одну границу веса")
        if (
            self.min_grams is not None
            and self.max_grams is not None
            and self.min_grams > self.max_grams
        ):
            raise ValueError("нижняя граница веса больше верхней")
        return self


class CargoValueCondition(_Strict):
    """Границы объявленной стоимости: минорные единицы и валюта (ADR-0011).

    **Валюта обязательна.** При её несовпадении с валютой запроса условие
    не совпадает, а не сравнивает числа: «50 000» в рублях и в тенге — разные
    величины, и сравнивать их запрещено (CLAUDE.md §6).
    """

    min_minor: int | None = Field(default=None, ge=0)
    max_minor: int | None = Field(default=None, ge=0)
    currency: str = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def _bounds_make_sense(self) -> CargoValueCondition:
        if not self.currency.isalpha():
            raise ValueError("код валюты должен быть тремя буквами ISO 4217")
        self.currency = self.currency.upper()
        if self.min_minor is None and self.max_minor is None:
            raise ValueError("укажите хотя бы одну границу стоимости")
        if (
            self.min_minor is not None
            and self.max_minor is not None
            and self.min_minor > self.max_minor
        ):
            raise ValueError("нижняя граница стоимости больше верхней")
        return self


class RuleConditions(_Strict):
    """По чему правило узнаёт «свой» запрос. Все указанные ключи — через И.

    Пустой объект ``{}`` — правило, применимое ко всему. Это законная запись:
    так выглядит «страховать всё» или «никогда не возить Почтой».
    """

    carrier: list[str] | None = Field(default=None, min_length=1)
    direction: DirectionCondition | None = None
    weight: WeightCondition | None = None
    cargo_value: CargoValueCondition | None = None
    cargo_type: list[CargoType] | None = Field(default=None, min_length=1)
    #: Опасность — вторая ось, а не значение ``CargoType``: опасным бывает
    #: и оборудование, и груз, и посылка (ADR-0028).
    dangerous: bool | None = None

    @model_validator(mode="after")
    def _carrier_codes_are_normalised(self) -> RuleConditions:
        if self.carrier is not None:
            codes = [code.strip().lower() for code in self.carrier]
            if not all(codes):
                raise ValueError("код перевозчика не может быть пустым")
            self.carrier = sorted(set(codes))
        return self

    @property
    def picks_carriers_only(self) -> bool:
        """Условие состоит из одного ``carrier`` и ничего больше."""
        return self.carrier is not None and not self._has_request_keys

    @property
    def _has_request_keys(self) -> bool:
        return any(
            value is not None
            for value in (
                self.direction,
                self.weight,
                self.cargo_value,
                self.cargo_type,
                self.dangerous,
            )
        )

    @property
    def restricts_by_cargo(self) -> bool:
        return self.cargo_type is not None or self.dangerous is not None


class RuleActions(_Strict):
    """Что правило делает. Ровно одно действие на правило.

    Не «хотя бы одно» и не «сколько угодно»: у правила есть имя, и оно
    показывается человеку строкой «запрещено правилом такого-то». Правило,
    делающее два дела сразу, такой строкой не объясняется, а разрешение
    противоречий устроено по видам действий — значит одно правило иначе
    участвовало бы в двух разных разборах.

    Логические действия принимают только ``true``: ``{"deny": false}`` —
    это правило, которое ничего не делает, и хранить его значит держать
    в политике строку, про которую никто не вспомнит, что она выключена
    не флагом ``enabled``, а значением действия.
    """

    deny: Literal[True] | None = None
    allow: Literal[True] | None = None
    require_insurance: Literal[True] | None = None
    auto_select: SelectionRule | None = None

    @model_validator(mode="after")
    def _exactly_one_action(self) -> RuleActions:
        chosen = [
            name
            for name, value in (
                ("deny", self.deny),
                ("allow", self.allow),
                ("require_insurance", self.require_insurance),
                ("auto_select", self.auto_select),
            )
            if value is not None
        ]
        if not chosen:
            raise ValueError("правило без действия ничего не делает: укажите одно действие")
        if len(chosen) > 1:
            raise ValueError("на правило приходится одно действие, указано: " + ", ".join(chosen))
        return self

    @property
    def kind(self) -> str:
        if self.deny is not None:
            return "deny"
        if self.allow is not None:
            return "allow"
        if self.require_insurance is not None:
            return "require_insurance"
        return "auto_select"


#: Действия, которые вправе смотреть на перевозчика. Остальные два решают
#: судьбу всей отправки: страхование входит в цену у всех, кого спрашивают,
#: а автовыбор — решение по выдаче целиком. Условие ``carrier`` рядом с ними
#: выглядело бы работающим и не значило бы ничего, поэтому оно отвергается
#: при записи, а не игнорируется при применении.
_CARRIER_AWARE_ACTIONS = frozenset({"deny", "allow"})


class RuleBody(_Strict):
    """Пара «условия и действия» — то, что проверяется при записи правила."""

    conditions: RuleConditions = Field(default_factory=RuleConditions)
    actions: RuleActions

    @model_validator(mode="after")
    def _carrier_condition_fits_the_action(self) -> RuleBody:
        if self.conditions.carrier is not None and self.actions.kind not in _CARRIER_AWARE_ACTIONS:
            raise ValueError(
                f"условие carrier не сочетается с действием {self.actions.kind}: "
                "это решение по всей отправке, а не по одному перевозчику"
            )
        return self


class RuleRow(Protocol):
    """То, что нужно разбору от строки таблицы ``routing_rules``.

    Протокол, а не сама модель: разбор не должен зависеть от SQLAlchemy,
    иначе его нельзя проверить без базы.
    """

    @property
    def id(self) -> UUID: ...
    @property
    def name(self) -> str: ...
    @property
    def priority(self) -> int: ...
    @property
    def conditions(self) -> dict[str, Any]: ...
    @property
    def actions(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ParsedRule:
    """Разобранное правило: то же самое, но в типах."""

    id: UUID
    name: str
    priority: int
    conditions: RuleConditions
    actions: RuleActions


@dataclass(frozen=True, slots=True)
class RequestFacts:
    """Свойства запроса, по которым правила узнают «свой» случай.

    Все шесть условий — свойства запроса, а не предложения: перевозчик,
    направление, вес, стоимость, тип груза и опасность известны до того,
    как хоть один перевозчик получил вызов. Поэтому правила решают, кого
    вообще спрашивать, а не какие строки погасить (ADR-0028).
    """

    #: Идентификаторы ФИАС городов отправления и назначения — те самые,
    #: которые платформа разрешила из адреса запроса. ``None`` означает
    #: «город не разрешён», и это НЕ совпадает ни с каким перечислением:
    #: применить правило «в Калининград — только Почтой» к запросу, про
    #: который мы не знаем, куда он едет, значило бы угадать за человека.
    origin_fias_id: UUID | None
    destination_fias_id: UUID | None
    #: Расчётный вес всего отправления в граммах, с учётом объёмного.
    billable_weight_grams: int
    cargo_value: Money
    cargo_type: CargoType
    dangerous: bool = False


@dataclass(frozen=True, slots=True)
class CarrierVerdict:
    """Что политика решила про одного перевозчика."""

    carrier_code: str
    allowed: bool
    reason: IneligibilityReason | None = None
    #: Название правила: строка «запрещено политикой» без имени правила
    #: не даёт логисту ни одного способа выяснить, каким именно.
    rule_name: str | None = None


@dataclass(frozen=True, slots=True)
class Policy:
    """Итог применения правил к одному запросу."""

    verdicts: tuple[CarrierVerdict, ...]
    require_insurance: bool = False
    insurance_rule: str | None = None
    auto_select: SelectionRule | None = None
    auto_select_rule: str | None = None

    @property
    def allowed_codes(self) -> tuple[str, ...]:
        return tuple(v.carrier_code for v in self.verdicts if v.allowed)

    @property
    def blocked(self) -> tuple[CarrierVerdict, ...]:
        """Запрещённые перевозчики: их не спрашивают, но и не прячут.

        Строка в выдаче остаётся — с причиной и без цены. Спрятать её
        значило бы показать клиенту выдачу, в которой платформа «ничего
        не нашла», умолчав, что нашла и не показала.
        """
        return tuple(v for v in self.verdicts if not v.allowed)

    def verdict(self, carrier_code: str) -> CarrierVerdict | None:
        for verdict in self.verdicts:
            if verdict.carrier_code == carrier_code:
                return verdict
        return None


def parse_rules(rows: list[RuleRow]) -> list[ParsedRule]:
    """Разобрать сохранённые правила, отсортировав по возрастанию приоритета.

    Нечитаемое правило останавливает разбор целиком. Пропустить его значило
    бы применить политику не полностью — и именно в ту сторону, в которую
    ошибаться нельзя: пропущенный запрет открывает то, что было закрыто.
    """
    parsed: list[ParsedRule] = []
    for row in rows:
        try:
            body = RuleBody.model_validate({"conditions": row.conditions, "actions": row.actions})
        except ValidationError as error:
            raise BrokenRoutingRule(
                f"Правило «{row.name}» не читается и не может быть применено"
            ) from error
        parsed.append(
            ParsedRule(
                id=row.id,
                name=row.name,
                priority=row.priority,
                conditions=body.conditions,
                actions=body.actions,
            )
        )
    # Порядок задаётся здесь, а не запросом: разрешение противоречий опирается
    # на приоритет, и молчаливая зависимость от порядка строк однажды
    # поменяла бы исход при изменении запроса, который к правилам отношения
    # не имеет.
    parsed.sort(key=lambda rule: rule.priority)
    return parsed


def evaluate(rules: list[ParsedRule], facts: RequestFacts, carrier_codes: list[str]) -> Policy:
    """Применить правила к запросу и набору подключённых перевозчиков."""
    applicable = [rule for rule in rules if _matches_request(rule.conditions, facts)]

    denies = [rule for rule in applicable if rule.actions.kind == "deny"]
    allows = [rule for rule in applicable if rule.actions.kind == "allow"]
    insurances = [rule for rule in applicable if rule.actions.kind == "require_insurance"]
    autos = [rule for rule in applicable if rule.actions.kind == "auto_select"]

    verdicts = tuple(_verdict(code, denies, allows) for code in carrier_codes)

    # Строгое побеждает мягкое: страхование обязательно, если его требует
    # хоть одно совпавшее правило. Имя берётся у сильнейшего — оно попадает
    # в объяснение, а объяснений должно быть одно.
    insurance_rule = insurances[-1].name if insurances else None
    auto = autos[-1] if autos else None

    return Policy(
        verdicts=verdicts,
        require_insurance=bool(insurances),
        insurance_rule=insurance_rule,
        auto_select=auto.actions.auto_select if auto else None,
        auto_select_rule=auto.name if auto else None,
    )


def _verdict(code: str, denies: list[ParsedRule], allows: list[ParsedRule]) -> CarrierVerdict:
    """Судьба одного перевозчика. Запрет сильнее разрешения — всегда.

    Запрет заводят по договорной или юридической причине: у перевозчика нет
    договора, он не прошёл проверку безопасности, ему нельзя опасный груз.
    Ошибка в сторону запрета стоит одной ручной отправки, ошибка в другую
    сторону — нарушения политики, о котором никто не узнает.
    """
    blocking = [rule for rule in denies if _matches_carrier(rule.conditions, code)]
    if blocking:
        rule = blocking[-1]
        return CarrierVerdict(
            carrier_code=code,
            allowed=False,
            reason=_deny_reason(rule.conditions),
            rule_name=rule.name,
        )

    # Whitelist — это не «разрешить вот это», а «кроме этого — ничего».
    # Но действует он, только если хоть одно ``allow``-правило совпало
    # с запросом: иначе правило «в Калининград — только Почтой» запрещало бы
    # всё и везде, а имелось в виду одно направление.
    #
    # Несколько совпавших белых списков СУЖАЮТ друг друга, а не складываются.
    # Пусть совпали «тяжелее 30 кг — только ДЛ и ПЭК» и «в Калининград —
    # только Почтой»: объединение разрешило бы всех троих, то есть нарушило
    # бы оба правила сразу. Пересечение честно отвечает «никем» — и это
    # разбор человеком, а не тихо выбранный вариант вопреки политике.
    missing = [rule for rule in allows if not _matches_carrier(rule.conditions, code)]
    if missing:
        return CarrierVerdict(
            carrier_code=code,
            allowed=False,
            reason=IneligibilityReason.NOT_IN_WHITELIST,
            rule_name=missing[-1].name,
        )

    return CarrierVerdict(carrier_code=code, allowed=True)


def _deny_reason(conditions: RuleConditions) -> IneligibilityReason:
    """Почему запрещено — в терминах ``IneligibilityReason``.

    Различаются не для красоты: «этот перевозчик у нас в чёрном списке»,
    «этому перевозчику нельзя такой груз» и «так решила политика компании» —
    три разных разговора с логистом.
    """
    if conditions.restricts_by_cargo:
        return IneligibilityReason.CARGO_RESTRICTED
    if conditions.picks_carriers_only:
        return IneligibilityReason.CARRIER_BLACKLISTED
    return IneligibilityReason.TENANT_POLICY


def _matches_request(conditions: RuleConditions, facts: RequestFacts) -> bool:
    """Совпало ли условие с запросом. Ключ ``carrier`` здесь не участвует."""
    if conditions.direction is not None and not _matches_direction(conditions.direction, facts):
        return False
    if conditions.weight is not None and not _matches_weight(
        conditions.weight, facts.billable_weight_grams
    ):
        return False
    if conditions.cargo_value is not None and not _matches_value(
        conditions.cargo_value, facts.cargo_value
    ):
        return False
    if conditions.cargo_type is not None and facts.cargo_type not in conditions.cargo_type:
        return False
    return conditions.dangerous is None or conditions.dangerous is facts.dangerous


def _matches_carrier(conditions: RuleConditions, carrier_code: str) -> bool:
    """Условие без ключа ``carrier`` относится ко всем перевозчикам."""
    if conditions.carrier is None:
        return True
    return carrier_code.strip().lower() in conditions.carrier


def _matches_direction(direction: DirectionCondition, facts: RequestFacts) -> bool:
    """Направление сравнивается по городам ФИАС.

    Неразрешённый город (``None``) НЕ совпадает с перечислением. Считать
    иначе значило бы применить правило «в Калининград — только Почтой»
    к запросу, про который мы не знаем, куда он едет.
    """
    if direction.from_ is not None and (
        facts.origin_fias_id is None or facts.origin_fias_id not in direction.from_
    ):
        return False
    return direction.to is None or (
        facts.destination_fias_id is not None and facts.destination_fias_id in direction.to
    )


def _matches_weight(weight: WeightCondition, grams: int) -> bool:
    """Границы включительно."""
    if weight.min_grams is not None and grams < weight.min_grams:
        return False
    return weight.max_grams is None or grams <= weight.max_grams


def _matches_value(condition: CargoValueCondition, value: Money) -> bool:
    """Стоимость сравнивается только внутри одной валюты.

    Несовпадение валют — это «условие не про этот запрос», а не «ноль против
    ста тысяч». Сравнить числа разных валют запрещено (CLAUDE.md §6), и
    молчаливое сравнение здесь дало бы запрет по правилу, которое человек
    писал про рубли.
    """
    if condition.currency != value.currency:
        return False
    if condition.min_minor is not None and value.amount_minor < condition.min_minor:
        return False
    return condition.max_minor is None or value.amount_minor <= condition.max_minor


#: Версия политики, когда у тенанта нет ни одного включённого правила.
#: Отсутствие правил — тоже политика, и в снимке она обязана быть названа:
#: пустое поле нельзя отличить от «версию забыли записать». Значение то же,
#: что было до ADR-0028: у пустого набора не должно появиться второе имя,
#: иначе одна и та же политика выглядит в истории двумя разными.
EMPTY_POLICY_VERSION = "default-1"

#: Приставка отпечатка политики. Читается в снимке рекомендации глазами,
#: поэтому у неё есть имя, а не просто шестнадцать шестнадцатеричных цифр.
_FINGERPRINT_PREFIX = "policy-"

#: Длина отпечатка. Колонка ``policy_version`` — ``String(40)``, и приставка
#: с шестнадцатью знаками в неё укладывается с запасом. Шестнадцать знаков
#: blake2b — это 64 бита: на десятках правил у одного тенанта совпадение
#: неотличимо от невозможного.
_FINGERPRINT_LENGTH = 16


def policy_fingerprint(rules: list[ParsedRule]) -> str:
    """Отпечаток всего включённого набора правил.

    Версия политики обязана различать наборы, а не правила: до ADR-0028 она
    бралась от правила с наибольшим приоритетом, и изменение любого другого
    правила версию не меняло. Два разных набора давали одинаковую версию —
    то есть по историческому снимку нельзя было понять, по каким правилам
    принято решение, а поле заведено ровно для этого.

    Отпечаток берётся от содержания, а не от времени: набор, приведённый
    к прежнему виду, обязан получить прежнюю версию, иначе одинаковые
    политики выглядят в аналитике разными.
    """
    if not rules:
        return EMPTY_POLICY_VERSION
    payload = [
        {
            "id": str(rule.id),
            "priority": rule.priority,
            "conditions": rule.conditions.model_dump(mode="json", by_alias=True, exclude_none=True),
            "actions": rule.actions.model_dump(mode="json", exclude_none=True),
        }
        for rule in sorted(rules, key=lambda rule: rule.priority)
    ]
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).hexdigest()
    return _FINGERPRINT_PREFIX + digest[:_FINGERPRINT_LENGTH]
