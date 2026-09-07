"""Decision Engine: рекомендация по расчёту и фиксация решения.

Модуль не вызывает перевозчиков и не считает стоимость: он работает на уже
полученных предложениях (ADR-0014). Благодаря этому рекомендацию можно
пересчитать на историческом снимке, ради чего ТЗ и требует хранить
``algorithm_version`` и ``policy_version``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.config import Settings, get_settings
from aerogram.rating.models import RateOffer, RateQuote
from aerogram.rating.repository import RateRepository
from aerogram.routing.explanation import alternatives_delta, build_facts, render
from aerogram.routing.models import Decision, Recommendation, RoutingRule
from aerogram.routing.repository import RoutingRepository
from aerogram.routing.rules import EMPTY_POLICY_VERSION, parse_rules, policy_fingerprint
from aerogram.routing.schemas import (
    AutoDecisionOut,
    DecisionOut,
    DecisionRequestIn,
    DecisionResponse,
    RecommendationOut,
    RoutingRequestIn,
    RoutingRuleIn,
    RoutingRuleOut,
    RoutingRulePatch,
    RoutingRulesOut,
)
from aerogram.routing.selection import SELECTION_VERSION, Abstention, select
from aerogram.routing.snapshot import load_policy_snapshot
from aerogram.routing.strategies import ALGORITHM_VERSION, OfferFacts, rank
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import DecisionMode, OverrideReason, RoutingStrategy, SelectionRule
from aerogram.shared.errors import AerogramError, Conflict, NotFound, ValidationFailed
from aerogram.shared.idempotency import ensure_same_request, request_fingerprint
from aerogram.shared.ids import uuid7
from aerogram.shared.logging import get_logger
from aerogram.shared.money import Money

__all__ = [
    "AUTO_KEY_PREFIX",
    "AutoSelectService",
    "AutoSelection",
    "AutoSkip",
    "DecisionService",
    "RecommendationService",
    "RoutingRuleService",
    "auto_idempotency_key",
]

log = get_logger(__name__)

#: Версия политики, когда у тенанта нет ни одного правила маршрутизации.
#: Живёт рядом с самим отпечатком (``routing.rules``): версия политики
#: должна вычисляться в одном месте, иначе пустой набор однажды получит
#: два разных имени.
DEFAULT_POLICY_VERSION = EMPTY_POLICY_VERSION

#: Префикс ключа идемпотентности автоматического решения. Зарезервирован:
#: клиент не вправе прислать такой ``Idempotency-Key`` на ``POST /v1/decisions``
#: и выдать своё решение за машинное (ADR-0029).
AUTO_KEY_PREFIX = "auto:"


def auto_idempotency_key(quote_id: UUID) -> str:
    """Ключ автоматического решения — от РАСЧЁТА, а не от рекомендации.

    Рекомендация не единственна: каждый ``POST /v1/routing/quote`` создаёт
    новую строку, и кабинет зовёт её при каждой смене вкладки стратегии.
    Ключ от рекомендации дал бы по решению на вкладку.
    """
    return f"{AUTO_KEY_PREFIX}{quote_id}"


@dataclass(frozen=True, slots=True)
class AutoSelection:
    """Чем сделан автоматический выбор — четыре колонки снимка решения.

    Имя правила лежит рядом с идентификатором: переименование правила
    не должно переписывать историю, а удаление — стирать её.
    """

    rule: SelectionRule
    rule_id: UUID
    rule_name: str
    version: str = SELECTION_VERSION


class AutoSkip(StrEnum):
    """Почему автоматического решения не появилось.

    Причина называется всегда. «Автовыбор не сработал» без причины — это
    ровно тот молчаливый ноль в доле решений без человека, который
    ADR-0029 и чинит.
    """

    DISABLED = "disabled"
    NO_SNAPSHOT = "no_snapshot"
    NO_RULE = "no_rule"
    OTHER_STRATEGY = "other_strategy"
    ALREADY_DECIDED = "already_decided"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AutoOutcome:
    """Итог попытки выбрать без человека. Заполнено ровно одно поле.

    Решение возвращается целиком, а не одним идентификатором: экран обязан
    показать, каким правилом и что именно выбрано, в тот же момент, что
    и рекомендацию. Иначе оператор нажмёт «Принять рекомендацию» и создаст
    по тому же расчёту второе решение, не зная о первом.
    """

    decision: Decision | None = None
    skipped: str | None = None


class RecommendationService:
    """Рекомендация по снимку расчёта и стратегии."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._rates = RateRepository(session)
        self._routing = RoutingRepository(session)

    async def recommend(
        self, payload: RoutingRequestIn, *, tenant_id: UUID, auto_select: bool = True
    ) -> RecommendationOut:
        """Построить и сохранить рекомендацию.

        Просроченный расчёт не рекомендуется: цены и сроки в нём уже могли
        измениться, а решение, принятое по устаревшему снимку, невозможно
        предъявить перевозчику.

        ``auto_select = False`` отключает автовыбор для этого вызова. Нужен
        массовому прогону: там стратегию для строк выбрал оператор, а ключ
        решения автовыбора выведен из расчёта, который две одинаковые строки
        списка делят между собой (ADR-0029).
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
            # Версия политики берётся ИЗ РАСЧЁТА, а не пересчитывается по
            # нынешним правилам: иначе снимок назвал бы политику, которая
            # этот расчёт не порождала (ADR-0029). Пересчёт остаётся только
            # для выдач, снятых до миграции 0014, — их окно не длиннее срока
            # жизни выдачи после выката.
            policy_version=quote.policy_version or await self._policy_version(),
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
        if not auto_select:
            return _to_out(recommendation)
        outcome = await AutoSelectService(self._session, self._settings).consider(
            quote, recommendation, tenant_id=tenant_id
        )
        return _to_out(recommendation, outcome.decision)

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
        selection: AutoSelection | None = None,
    ) -> DecisionResponse:
        """Принять решение. Повтор с тем же ключом не создаёт второго решения.

        ``selection`` заполняется только автовыбором и попадает в снимок
        решения четырьмя колонками. Автоматический путь проходит буквально
        этот же метод, а не свою копию: разойдись они, ручное и машинное
        решения перестали бы одинаково проверяться, и разница обнаружилась бы
        на споре с клиентом.
        """
        if selection is not None and payload.mode is not DecisionMode.AUTO:
            # Ошибка вызывающего кода, а не запроса: то же держит CHECK
            # в схеме, но упасть здесь понятнее, чем ловить отказ базы.
            raise ValueError("снимок автовыбора допустим только у решения mode=auto")
        if selection is None and idempotency_key.startswith(AUTO_KEY_PREFIX):
            # Префикс зарезервирован за автовыбором. Иначе клиент занял бы
            # ключ будущего автоматического решения по этому расчёту — или
            # выдал бы своё решение за машинное, исказив долю автовыбора.
            raise ValidationFailed(
                f"Префикс «{AUTO_KEY_PREFIX}» зарезервирован платформой",
                field="Idempotency-Key",
            )
        if selection is None and payload.override_reason is OverrideReason.AUTO_SELECT_RULE:
            # По той же причине зарезервирована и причина: значение заведено,
            # чтобы отличать выбор правила от мотива человека (ADR-0029),
            # и разрешить человеку на него сослаться значит стереть разницу
            # ровно там, где она заводилась.
            raise ValidationFailed(
                "Эту причину проставляет правило автовыбора, а не человек",
                field="override_reason",
            )
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

        offer = await self._validated_offer(recommendation, payload.selected_offer_id)

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
            selection_rule=selection.rule if selection else None,
            auto_select_rule_id=selection.rule_id if selection else None,
            auto_select_rule_name=selection.rule_name if selection else None,
            selection_version=selection.version if selection else None,
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
            rule=selection.rule_name if selection else None,
        )
        return DecisionResponse(
            decision_id=decision.id,
            snapshot_id=recommendation.quote_id,
            created_at=decision.decided_at,
        )

    async def get(self, decision_id: UUID) -> DecisionOut:
        """Снимок принятого решения.

        Чужое решение RLS не отдаёт вовсе, и это тот же 404: наличие объекта
        у соседнего тенанта — не то, что стоит подтверждать (CLAUDE.md §6).
        """
        decision = await self._routing.get_decision(decision_id)
        if decision is None:
            raise NotFound("Решение не найдено")
        recommendation = await self._routing.get_recommendation(decision.recommendation_id)
        if recommendation is None:
            # Рекомендация решения — обязательная ссылка схемы. Её отсутствие
            # означало бы, что решение объяснить нечем; молча подставить
            # что-нибудь на её место было бы хуже отказа.
            raise NotFound("Рекомендация решения не найдена")
        return DecisionOut(
            id=decision.id,
            recommendation_id=decision.recommendation_id,
            quote_id=recommendation.quote_id,
            selected_offer_id=decision.selected_offer_id,
            mode=DecisionMode(decision.mode),
            actor_id=decision.actor_id,
            override=decision.override,
            override_reason=(
                OverrideReason(decision.override_reason) if decision.override_reason else None
            ),
            override_comment=decision.override_comment,
            selection_rule=(
                SelectionRule(decision.selection_rule) if decision.selection_rule else None
            ),
            auto_select_rule_id=decision.auto_select_rule_id,
            auto_select_rule_name=decision.auto_select_rule_name,
            selection_version=decision.selection_version,
            decided_at=decision.decided_at,
        )

    async def _validated_offer(self, recommendation: Recommendation, offer_id: UUID) -> RateOffer:
        """Проверить выбранное предложение четырьмя жёсткими условиями.

        Метод отдельный, потому что через него ходят оба пути — ручной
        и автоматический. Заведи автовыбор свою копию проверок, они однажды
        разошлись бы, и правило смогло бы выбрать то, чего не может выбрать
        человек.
        """
        offer = await self._rates.get_offer(offer_id)
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
        return offer


class AutoSelectService:
    """Автовыбор: замороженный вердикт политики доводится до решения.

    Своего выбора у сервиса нет — он исполняет правило, записанное
    в снимок расчёта, и создаёт решение тем же ``DecisionService``, каким
    его создаёт человек. Ошибка здесь не должна ломать рекомендацию:
    оператор попросил рекомендацию, а не автовыбор, и отдать ему ``500``
    вместо списка предложений — худший из возможных исходов.
    """

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._routing = RoutingRepository(session)
        self._decisions = DecisionService(session)

    async def consider(
        self, quote: RateQuote, recommendation: Recommendation, *, tenant_id: UUID
    ) -> AutoOutcome:
        """Принять решение по правилу автовыбора, если оно есть и применимо."""
        if not self._settings.auto_select_enabled:
            return self._skip(AutoSkip.DISABLED, quote)

        snapshot = load_policy_snapshot(quote.policy_snapshot)
        if snapshot is None:
            return self._skip(AutoSkip.NO_SNAPSHOT, quote)
        if (
            snapshot.auto_select is None
            or snapshot.auto_select_rule_id is None
            or snapshot.auto_select_rule is None
        ):
            # Все три части нужны разом: значение правила, его идентификатор
            # и имя. Снимок с пустым именем прошёл бы ``CHECK`` таблицы
            # (пустая строка не ``NULL``) и объяснял бы решение никак.
            return self._skip(AutoSkip.NO_RULE, quote)
        if recommendation.strategy != quote.strategy:
            # Гейт по стратегии: автовыбор срабатывает на рекомендации,
            # построенной по стратегии самого расчёта. Иначе ``override``
            # зависел бы от того, на какую вкладку человек нажал первой.
            return self._skip(AutoSkip.OTHER_STRATEGY, quote)

        key = auto_idempotency_key(quote.id)
        decided = await self._routing.decision_by_key(key)
        if decided is not None:
            # Проверяем ДО построения тела: у второй рекомендации по тому же
            # расчёту другой ``recommendation_id``, и обычная идемпотентность
            # увидела бы другое тело под тем же ключом и ответила 409.
            #
            # Решение при этом ОТДАЁТСЯ, а не молча пропускается: вторая
            # рекомендация по тому же расчёту — это обновлённый экран, и он
            # обязан показать, что выбор уже сделан.
            return AutoOutcome(decision=decided)

        chosen = select(
            [_facts(offer) for offer in quote.offers],
            snapshot.auto_select,
            deadline_set=quote.deadline is not None,
        )
        if chosen.offer is None:
            # Воздержание — законный исход, и у него названа причина.
            # ``or`` здесь только ради типов: ``Selection`` гарантирует
            # причину, когда предложения нет, и это держит отдельный тест.
            reason = chosen.abstention or Abstention.NO_ELIGIBLE_OFFERS
            return self._skip(reason.value, quote, rule=snapshot.auto_select_rule)

        is_override = chosen.offer.offer_id != recommendation.recommended_offer_id
        payload = DecisionRequestIn(
            recommendation_id=recommendation.id,
            selected_offer_id=chosen.offer.offer_id,
            override=is_override,
            # Расхождение правила со стратегией — нормальный исход, а не
            # ошибка: словари ``SelectionRule`` и ``RoutingStrategy``
            # не пересекаются (ADR-0029). Причина названа своим значением,
            # а не ``corporate_policy``: то — мотив человека.
            override_reason=OverrideReason.AUTO_SELECT_RULE if is_override else None,
            mode=DecisionMode.AUTO,
        )
        selection = AutoSelection(
            rule=snapshot.auto_select,
            rule_id=snapshot.auto_select_rule_id,
            rule_name=snapshot.auto_select_rule,
        )
        try:
            response = await self._decisions.decide(
                payload,
                tenant_id=tenant_id,
                user_id=None,
                idempotency_key=key,
                selection=selection,
            )
        except AerogramError as error:
            # Устаревшее предложение, гонка по ключу, отвергнутый выбор:
            # рекомендация от этого не перестаёт быть верной, и оператор
            # обязан её увидеть. Причина попадает в лог, а не в ответ.
            log.warning(
                "routing.auto_select_rejected",
                quote_id=str(quote.id),
                error_code=error.code,
            )
            return AutoOutcome(skipped=AutoSkip.REJECTED.value)

        log.info(
            "routing.auto_selected",
            quote_id=str(quote.id),
            decision_id=str(response.decision_id),
            rule=snapshot.auto_select.value,
            rule_name=snapshot.auto_select_rule,
            override=is_override,
            selection_version=SELECTION_VERSION,
        )
        created = await self._routing.get_decision(response.decision_id)
        # Строка уже в сессии после ``flush``: это чтение из карты
        # идентичности, а не второй запрос в базу.
        return AutoOutcome(decision=created)

    def _skip(self, reason: str, quote: RateQuote, *, rule: str | None = None) -> AutoOutcome:
        if reason != AutoSkip.DISABLED:
            # Выключенный рубильник — не событие: он молчит по всей выдаче
            # каждого тенанта. Остальные причины пишутся: правило заведено,
            # человек его ждёт, и «ничего не произошло» без причины
            # неотличимо от поломки.
            log.info(
                "routing.auto_select_skipped", quote_id=str(quote.id), reason=reason, rule=rule
            )
        return AutoOutcome(skipped=reason)


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


def _to_out(recommendation: Recommendation, decision: Decision | None = None) -> RecommendationOut:
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
        auto_decision=_auto_out(decision),
    )


def _auto_out(decision: Decision | None) -> AutoDecisionOut | None:
    """Решение автовыбора для экрана — или ``None``.

    Половина снимка сюда не проходит: ограничение таблицы держит «либо все
    четыре поля, либо ни одного», и полагаться на это в типах честнее,
    чем показывать правило без имени.
    """
    if (
        decision is None
        or decision.selection_rule is None
        or decision.auto_select_rule_id is None
        or decision.auto_select_rule_name is None
        or decision.selection_version is None
    ):
        return None
    return AutoDecisionOut(
        decision_id=decision.id,
        selected_offer_id=decision.selected_offer_id,
        rule=SelectionRule(decision.selection_rule),
        rule_id=decision.auto_select_rule_id,
        rule_name=decision.auto_select_rule_name,
        override=decision.override,
        selection_version=decision.selection_version,
        decided_at=decision.decided_at,
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
