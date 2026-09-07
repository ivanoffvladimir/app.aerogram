"""DTO Decision Engine. Соответствуют схемам ``docs/tz/v3/openapi.yaml``."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aerogram.routing.rules import RuleActions, RuleBody, RuleConditions
from aerogram.shared.enums import (
    DecisionMode,
    OverrideReason,
    RoutingStrategy,
    ScoreConfidence,
    SelectionRule,
)

__all__ = [
    "AutoDecisionOut",
    "DecisionOut",
    "DecisionRequestIn",
    "DecisionResponse",
    "RecommendationOut",
    "RoutingRequestIn",
    "RoutingRuleIn",
    "RoutingRuleOut",
    "RoutingRulePatch",
    "RoutingRulesOut",
]


class RoutingRequestIn(BaseModel):
    """Запрос рекомендации по уже полученному расчёту (схема ``RoutingRequest``)."""

    quote_id: UUID
    strategy: RoutingStrategy


class AutoDecisionOut(BaseModel):
    """Решение, принятое правилом автовыбора, без человека (ADR-0029).

    Едет вместе с рекомендацией, потому что экран обязан узнать о нём
    в тот же момент: иначе оператор нажмёт «Принять рекомендацию» и создаст
    по тому же расчёту ВТОРОЕ решение, не зная о первом.

    Правило названо и значением, и именем: значение объясняет выбор,
    имя — чьё это правило. Имя историческое, из снимка: правило могли
    переименовать после решения, и подставлять сегодняшнее имя значило бы
    переписывать историю.
    """

    decision_id: UUID
    selected_offer_id: UUID
    rule: SelectionRule
    rule_id: UUID
    rule_name: str
    #: Правило выбрало не то, что рекомендовала стратегия. Нормальный исход:
    #: словари ``SelectionRule`` и ``RoutingStrategy`` не пересекаются.
    override: bool
    selection_version: str
    decided_at: datetime


class RecommendationOut(BaseModel):
    """Рекомендация (схема ``Recommendation``).

    ``explanation`` — строки: так требует контракт. В базе рядом лежат
    структурированные факты, из которых эти строки собраны, — чтобы
    объяснение можно было переформулировать и посчитать по нему аналитику.
    """

    id: UUID
    quote_id: UUID
    strategy: RoutingStrategy
    recommended_offer_id: UUID | None
    explanation: list[str]
    algorithm_version: str
    policy_version: str
    alternatives_delta: dict[str, Any] = Field(default_factory=dict)
    confidence: ScoreConfidence | None = None
    #: Решение, уже принятое правилом автовыбора по этому расчёту.
    #: ``None`` — правила нет, оно не сработало или рубильник выключен;
    #: во всех трёх случаях выбор остаётся за человеком.
    auto_decision: AutoDecisionOut | None = None


class DecisionOut(BaseModel):
    """Снимок принятого решения — то, чем оно объясняется через год.

    Пути в замороженном контракте нет: там описано только создание решения.
    Прочитать его, однако, нужно и карточке отправления, и разбору спора,
    и снимок автовыбора виден иначе только в базе.
    """

    id: UUID
    recommendation_id: UUID
    quote_id: UUID
    selected_offer_id: UUID
    mode: DecisionMode
    #: Автор. У машинного решения его нет — это не пропуск, а утверждение.
    actor_id: UUID | None
    override: bool
    override_reason: OverrideReason | None
    override_comment: str | None
    selection_rule: SelectionRule | None
    auto_select_rule_id: UUID | None
    auto_select_rule_name: str | None
    selection_version: str | None
    decided_at: datetime


class DecisionRequestIn(BaseModel):
    """Подтверждение выбора (схема ``DecisionRequest``)."""

    recommendation_id: UUID
    selected_offer_id: UUID
    override: bool = False
    override_reason: OverrideReason | None = None
    override_comment: str | None = Field(default=None, max_length=2000)
    mode: DecisionMode = DecisionMode.MANUAL

    @model_validator(mode="after")
    def _override_states_its_reason(self) -> DecisionRequestIn:
        """Причина обязательна при override — как и в схеме БД.

        Проверка дублирует ограничение таблицы намеренно: клиент должен
        получить понятную ошибку поля, а не отказ базы данных.
        """
        if self.override and self.override_reason is None:
            raise ValueError("при выборе не рекомендованного варианта нужна причина")
        return self


class DecisionResponse(BaseModel):
    """Созданное решение (схема ``DecisionResponse``).

    ``snapshot_id`` — идентификатор снимка расчёта, на котором принято решение.
    Именно он делает решение воспроизводимым: по нему поднимаются все
    предложения в том виде, в каком их видел оператор.
    """

    decision_id: UUID
    snapshot_id: UUID
    created_at: datetime


class RoutingRuleIn(RuleBody):
    """Новое правило маршрутизации.

    Наследуется от ``RuleBody`` намеренно: состав условий и действий
    проверяется ровно тем же кодом, что применяется при расчёте. Опиши мы
    их здесь заново, две проверки однажды разошлись бы — и правило,
    принятое на запись, перестало бы читаться при применении.
    """

    name: str = Field(min_length=1, max_length=255)
    #: Больший приоритет — сильнее. Уникален внутри тенанта: два правила
    #: с одинаковым приоритетом дали бы разный исход при разном порядке
    #: чтения строк.
    priority: int = Field(ge=0)
    enabled: bool = True


class RoutingRulePatch(BaseModel):
    """Правка правила. Не переданное поле не меняется.

    ``conditions`` и ``actions`` меняются только парой: действие без своего
    условия и условие без своего действия — это половина правила, и та
    половина, что осталась от прежнего, почти наверняка означает не то,
    что человек имел в виду.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int | None = Field(default=None, ge=0)
    enabled: bool | None = None
    conditions: RuleConditions | None = None
    actions: RuleActions | None = None

    @model_validator(mode="after")
    def _conditions_and_actions_change_together(self) -> RoutingRulePatch:
        if (self.conditions is None) != (self.actions is None):
            raise ValueError("условия и действия правила меняются только вместе")
        if self.conditions is not None and self.actions is not None:
            # Та же проверка сочетаемости, что и при создании: иначе правку
            # можно было бы провести мимо неё.
            RuleBody(conditions=self.conditions, actions=self.actions)
        return self


class RoutingRuleOut(BaseModel):
    """Правило маршрутизации в кабинете."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    priority: int
    enabled: bool
    conditions: dict[str, Any]
    actions: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class RoutingRulesOut(BaseModel):
    """Список правил и версия политики, которую они образуют.

    Версия показывается рядом со списком, потому что она попадает в снимок
    каждого решения: без неё нельзя понять, та ли это политика, по которой
    принято решение месяц назад.
    """

    items: list[RoutingRuleOut]
    policy_version: str
