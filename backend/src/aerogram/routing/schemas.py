"""DTO Decision Engine. Соответствуют схемам ``docs/tz/v3/openapi.yaml``."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from aerogram.shared.enums import (
    DecisionMode,
    OverrideReason,
    RoutingStrategy,
    ScoreConfidence,
)

__all__ = [
    "DecisionRequestIn",
    "DecisionResponse",
    "RecommendationOut",
    "RoutingRequestIn",
]


class RoutingRequestIn(BaseModel):
    """Запрос рекомендации по уже полученному расчёту (схема ``RoutingRequest``)."""

    quote_id: UUID
    strategy: RoutingStrategy


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
