"""Репозиторий Decision Engine. Единственное место с SQL в модуле."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.routing.models import Decision, Recommendation, RoutingRule

__all__ = ["RoutingRepository"]


class RoutingRepository:
    """Рекомендации, решения и правила маршрутизации."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add_recommendation(self, recommendation: Recommendation) -> Recommendation:
        self._session.add(recommendation)
        return recommendation

    async def get_recommendation(self, recommendation_id: UUID) -> Recommendation | None:
        return await self._session.get(Recommendation, recommendation_id)

    def add_decision(self, decision: Decision) -> Decision:
        self._session.add(decision)
        return decision

    async def get_decision(self, decision_id: UUID) -> Decision | None:
        return await self._session.get(Decision, decision_id)

    async def decision_by_key(self, idempotency_key: str) -> Decision | None:
        """Решение по ключу идемпотентности.

        Тенант в условии не указывается: его обеспечивает RLS, а ключ
        уникален в пределах тенанта. Явный фильтр здесь дублировал бы
        политику и создавал ложное впечатление, что без него утечёт.
        """
        stmt = select(Decision).where(Decision.idempotency_key == idempotency_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def add_rule(self, rule: RoutingRule) -> RoutingRule:
        self._session.add(rule)
        return rule

    async def get_rule(self, rule_id: UUID) -> RoutingRule | None:
        return await self._session.get(RoutingRule, rule_id)

    async def delete_rule(self, rule: RoutingRule) -> None:
        await self._session.delete(rule)

    async def all_rules(self) -> list[RoutingRule]:
        """Все правила тенанта, включая выключенные, по возрастанию приоритета.

        Кабинету нужны и выключенные: правило, которое не видно, нельзя ни
        включить обратно, ни удалить, — и оно останется висеть в таблице
        с занятым приоритетом.
        """
        stmt = select(RoutingRule).order_by(RoutingRule.priority)
        return list((await self._session.execute(stmt)).scalars())

    async def rule_by_priority(self, priority: int) -> RoutingRule | None:
        """Правило с этим приоритетом. Уникальность проверяется до записи.

        Ограничение таблицы всё равно не пропустит второе, но отдать
        человеку «нарушение уникальности» вместо «этот приоритет занят
        правилом такого-то» значит переложить на него чтение ошибки БД.
        """
        stmt = select(RoutingRule).where(RoutingRule.priority == priority)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def active_rules(self) -> list[RoutingRule]:
        """Включённые правила тенанта по возрастанию приоритета."""
        stmt = (
            select(RoutingRule).where(RoutingRule.enabled.is_(True)).order_by(RoutingRule.priority)
        )
        return list((await self._session.execute(stmt)).scalars())
