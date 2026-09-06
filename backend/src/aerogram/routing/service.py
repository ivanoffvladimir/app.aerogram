"""Decision Engine: рекомендация по расчёту и фиксация решения.

Модуль не вызывает перевозчиков и не считает стоимость: он работает на уже
полученных предложениях (ADR-0014). Благодаря этому рекомендацию можно
пересчитать на историческом снимке, ради чего ТЗ и требует хранить
``algorithm_version`` и ``policy_version``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.rating.models import RateOffer
from aerogram.rating.repository import RateRepository
from aerogram.routing.explanation import alternatives_delta, build_facts, render
from aerogram.routing.models import Decision, Recommendation, RoutingRule
from aerogram.routing.repository import RoutingRepository
from aerogram.routing.rules import EMPTY_POLICY_VERSION, parse_rules, policy_fingerprint
from aerogram.routing.schemas import (
    DecisionRequestIn,
    DecisionResponse,
    RecommendationOut,
    RoutingRequestIn,
    RoutingRuleIn,
    RoutingRuleOut,
    RoutingRulePatch,
    RoutingRulesOut,
)
from aerogram.routing.strategies import ALGORITHM_VERSION, OfferFacts, rank
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import DecisionMode, RoutingStrategy
from aerogram.shared.errors import Conflict, NotFound, ValidationFailed
from aerogram.shared.idempotency import ensure_same_request, request_fingerprint
from aerogram.shared.ids import uuid7
from aerogram.shared.logging import get_logger
from aerogram.shared.money import Money

__all__ = ["DecisionService", "RecommendationService", "RoutingRuleService"]

log = get_logger(__name__)

#: Версия политики, когда у тенанта нет ни одного правила маршрутизации.
#: Живёт рядом с самим отпечатком (``routing.rules``): версия политики
#: должна вычисляться в одном месте, иначе пустой набор однажды получит
#: два разных имени.
DEFAULT_POLICY_VERSION = EMPTY_POLICY_VERSION


class RecommendationService:
    """Рекомендация по снимку расчёта и стратегии."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._rates = RateRepository(session)
        self._routing = RoutingRepository(session)

    async def recommend(self, payload: RoutingRequestIn, *, tenant_id: UUID) -> RecommendationOut:
        """Построить и сохранить рекомендацию.

        Просроченный расчёт не рекомендуется: цены и сроки в нём уже могли
        измениться, а решение, принятое по устаревшему снимку, невозможно
        предъявить перевозчику.
        """
        quote = await self._rates.get_quote(payload.quote_id)
        if quote is None:
            # Чужой снимок RLS не отдаёт вовсе, и это тот же 404: наличие
            # объекта у соседнего тенанта — не то, что стоит подтверждать.
            raise NotFound("Расчёт не найден")
        if quote.valid_until <= utcnow():
            raise Conflict("Расчёт устарел, требуется пересчёт", field="quote_id")

        facts = [_facts(offer) for offer in quote.offers]
        ranking = rank(facts, payload.strategy)
        best = ranking.best

        explanation = [f.as_json() for f in build_facts(ranking, payload.strategy)]
        recommendation = Recommendation(
            id=uuid7(),
            tenant_id=tenant_id,
            quote_id=quote.id,
            recommended_offer_id=best.offer_id if best else None,
            strategy=payload.strategy,
            explanation=explanation,
            alternatives_delta=alternatives_delta(ranking) or None,
            algorithm_version=ALGORITHM_VERSION,
            policy_version=await self._policy_version(),
            confidence=ranking.confidence,
        )
        self._routing.add_recommendation(recommendation)
        await self._session.flush()

        log.info(
            "routing.recommended",
            strategy=payload.strategy.value,
            eligible=len([f for f in facts if f.eligible]),
            recommended=best is not None,
            confidence=ranking.confidence.value,
        )
        return _to_out(recommendation)

    async def _policy_version(self) -> str:
        """Версия политики тенанта на момент рекомендации.

        Отпечаток всего включённого набора, а не поле правила с наибольшим
        приоритетом, как было до ADR-0028: тогда изменение любого другого
        правила версию не меняло, два разных набора давали одинаковую
        версию — и по историческому снимку нельзя было понять, по каким
        правилам принято решение. А поле заведено ровно для этого
        (продуктовое ТЗ, раздел 8).
        """
        return policy_fingerprint(parse_rules(list(await self._routing.active_rules())))


class DecisionService:
    """Фиксация решения — неизменяемого снимка выбора."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._rates = RateRepository(session)
        self._routing = RoutingRepository(session)

    async def decide(
        self,
        payload: DecisionRequestIn,
        *,
        tenant_id: UUID,
        user_id: UUID | None,
        idempotency_key: str,
    ) -> DecisionResponse:
        """Принять решение. Повтор с тем же ключом не создаёт второго решения."""
        body = payload.model_dump(mode="json")
        existing = await self._routing.decision_by_key(idempotency_key)
        if existing is not None:
            ensure_same_request(existing.request_fingerprint, body)
            recommendation = await self._routing.get_recommendation(existing.recommendation_id)
            return DecisionResponse(
                decision_id=existing.id,
                snapshot_id=recommendation.quote_id if recommendation else existing.id,
                created_at=existing.decided_at,
            )

        recommendation = await self._routing.get_recommendation(payload.recommendation_id)
        if recommendation is None:
            raise NotFound("Рекомендация не найдена")

        offer = await self._rates.get_offer(payload.selected_offer_id)
        if offer is None:
            raise NotFound("Предложение не найдено")
        if offer.quote_id != recommendation.quote_id:
            # Иначе решение ссылалось бы на предложение из другого расчёта,
            # и снимок перестал бы объяснять сам себя.
            raise ValidationFailed(
                "Предложение относится к другому расчёту", field="selected_offer_id"
            )
        if offer.valid_until <= utcnow():
            raise Conflict("Предложение устарело, требуется пересчёт", field="selected_offer_id")
        if not offer.eligible:
            # Жёсткое ограничение остаётся жёстким и при ручном выборе:
            # оператор не должен уметь выбрать вариант, нарушающий дедлайн,
            # не пересчитав расчёт без дедлайна.
            raise ValidationFailed(
                "Это предложение не проходит по заданным ограничениям",
                field="selected_offer_id",
            )

        is_override = offer.id != recommendation.recommended_offer_id
        if is_override and payload.override_reason is None:
            raise ValidationFailed(
                "Выбран не рекомендованный вариант: нужна причина",
                field="override_reason",
            )

        if payload.mode is DecisionMode.MANUAL and user_id is None:
            # У ручного решения обязан быть автор — это ограничение схемы.
            # Клиент по API-ключу человеком не является, и его решение
            # по смыслу автоматическое. Молча подменять режим нельзя:
            # это исказило бы долю автовыбора в аналитике, ради которой
            # поле и существует. Поэтому говорим прямо, что прислать.
            raise ValidationFailed(
                "Решение без пользователя считается автоматическим: "
                'клиенту по API-ключу нужно указать mode="auto"',
                field="mode",
            )

        decision = Decision(
            id=uuid7(),
            tenant_id=tenant_id,
            recommendation_id=recommendation.id,
            selected_offer_id=offer.id,
            actor_id=user_id if payload.mode is DecisionMode.MANUAL else None,
            mode=payload.mode,
            override=is_override,
            override_reason=payload.override_reason if is_override else None,
            override_comment=payload.override_comment if is_override else None,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(body),
        )
        self._routing.add_decision(decision)
        await self._session.flush()

        log.info(
            "routing.decided",
            mode=payload.mode.value,
            override=is_override,
            reason=payload.override_reason.value if payload.override_reason else None,
        )
        return DecisionResponse(
            decision_id=decision.id,
            snapshot_id=recommendation.quote_id,
            created_at=decision.decided_at,
        )


def _facts(offer: RateOffer) -> OfferFacts:
    """Строка расчёта → факты для стратегии.

    Строка ошибки перевозчика приходит сюда с ``eligible = False`` и в выбор
    не попадает: у неё нет цены, и подставить ноль значило бы вывести её
    первой как самую дешёвую.
    """
    return OfferFacts(
        offer_id=offer.id,
        carrier_id=offer.carrier_id,
        total=Money(offer.total_amount_minor or 0, offer.currency),
        eta=offer.eta,
        eligible=offer.eligible and offer.total_amount_minor is not None,
        on_time_probability=offer.on_time_probability,
        risk=offer.risk,
        carrier_score=offer.score_at_quote,
        deadline_margin_seconds=offer.deadline_margin_seconds,
        lateness_seconds=offer.lateness_seconds,
    )


def _to_out(recommendation: Recommendation) -> RecommendationOut:
    return RecommendationOut(
        id=recommendation.id,
        quote_id=recommendation.quote_id,
        strategy=RoutingStrategy(recommendation.strategy),
        recommended_offer_id=recommendation.recommended_offer_id,
        explanation=render(recommendation.explanation),
        algorithm_version=recommendation.algorithm_version,
        policy_version=recommendation.policy_version,
        alternatives_delta=recommendation.alternatives_delta or {},
        confidence=recommendation.confidence,
    )


class RoutingRuleService:
    """Правила маршрутизации: чтение и правка корпоративной политики.

    Версия политики пересчитывается при каждом изменении и записывается
    во ВСЕ правила тенанта. Колонка ``policy_version`` при этом не второй
    источник истины, а материализация: рекомендация считает отпечаток
    от самого набора, а тест сверяет их равенство. Разойдись они, это
    увидит тест, а не аналитик через полгода.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._routing = RoutingRepository(session)

    async def list(self) -> RoutingRulesOut:
        rules = await self._routing.all_rules()
        return RoutingRulesOut(
            items=[RoutingRuleOut.model_validate(rule) for rule in rules],
            policy_version=await self._version(),
        )

    async def create(self, payload: RoutingRuleIn, *, tenant_id: UUID) -> RoutingRuleOut:
        await self._priority_is_free(payload.priority)
        rule = RoutingRule(
            id=uuid7(),
            tenant_id=tenant_id,
            name=payload.name,
            priority=payload.priority,
            enabled=payload.enabled,
            conditions=payload.conditions.model_dump(mode="json", by_alias=True, exclude_none=True),
            actions=payload.actions.model_dump(mode="json", exclude_none=True),
            policy_version=EMPTY_POLICY_VERSION,
        )
        self._routing.add_rule(rule)
        await self._session.flush()
        await self._restamp()
        log.info("routing.rule_created", rule_id=str(rule.id), priority=rule.priority)
        return RoutingRuleOut.model_validate(rule)

    async def update(self, rule_id: UUID, payload: RoutingRulePatch) -> RoutingRuleOut:
        rule = await self._rule(rule_id)
        if payload.priority is not None and payload.priority != rule.priority:
            await self._priority_is_free(payload.priority)
            rule.priority = payload.priority
        if payload.name is not None:
            rule.name = payload.name
        if payload.enabled is not None:
            rule.enabled = payload.enabled
        if payload.conditions is not None and payload.actions is not None:
            rule.conditions = payload.conditions.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            rule.actions = payload.actions.model_dump(mode="json", exclude_none=True)
        await self._session.flush()
        await self._restamp()
        log.info("routing.rule_updated", rule_id=str(rule.id))
        return RoutingRuleOut.model_validate(rule)

    async def delete(self, rule_id: UUID) -> None:
        rule = await self._rule(rule_id)
        await self._routing.delete_rule(rule)
        await self._session.flush()
        await self._restamp()
        log.info("routing.rule_deleted", rule_id=str(rule_id))

    async def _rule(self, rule_id: UUID) -> RoutingRule:
        rule = await self._routing.get_rule(rule_id)
        if rule is None:
            # Чужое правило RLS не отдаёт вовсе, и это тот же 404: наличие
            # объекта у соседнего тенанта — не то, что стоит подтверждать.
            raise NotFound("Правило не найдено")
        return rule

    async def _priority_is_free(self, priority: int) -> None:
        taken = await self._routing.rule_by_priority(priority)
        if taken is not None:
            raise Conflict(f"Приоритет {priority} занят правилом «{taken.name}»", field="priority")

    async def _version(self) -> str:
        return policy_fingerprint(parse_rules(list(await self._routing.active_rules())))

    async def _restamp(self) -> None:
        """Записать новую версию политики во все правила тенанта.

        Во все, а не только в изменённое: версия описывает НАБОР, и правило,
        сохранившее прежнюю, утверждало бы, что политика не менялась.
        """
        version = await self._version()
        for rule in await self._routing.all_rules():
            rule.policy_version = version
        await self._session.flush()
