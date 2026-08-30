"""DTO Carrier Intelligence."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from aerogram.shared.enums import ScoreConfidence, ScoreScope

__all__ = ["CarrierAnalyticsOut", "ScoreComponentsOut"]


class ScoreComponentsOut(BaseModel):
    """Расшифровка скора по компонентам (FR-7.5).

    Без неё скор — непрозрачное число, которому оператор не обязан верить.
    """

    on_time_rate: Decimal | None = None
    reliability: Decimal | None = None
    incident_rate: Decimal | None = None
    price_index: Decimal | None = None
    data_quality: Decimal | None = None


class CarrierAnalyticsOut(BaseModel):
    """Скор перевозчика с указанием, откуда он взят.

    ``score is None`` при ``confidence = insufficient`` — это не ошибка,
    а обязательное поведение (FR-7.3): интерфейс показывает «недостаточно
    данных», а не число.
    """

    carrier_id: UUID
    carrier_code: str
    carrier_name: str
    score: int | None
    confidence: ScoreConfidence
    #: Разрез, из которого взято значение. Показывается пользователю:
    #: глобальный скор и скор по направлению — разные утверждения.
    scope_type: ScoreScope | None = None
    scope_key: str = ""
    sample_size: int = 0
    period_start: date | None = None
    period_end: date | None = None
    components: ScoreComponentsOut = ScoreComponentsOut()
    formula_version: str | None = None
    calculated_at: datetime | None = None
