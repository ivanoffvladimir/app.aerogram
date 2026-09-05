"""DTO Carrier Intelligence."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from aerogram.shared.enums import ScoreBasis, ScoreConfidence, ScoreScope

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
    данных», а не число. С появлением платформенной базы (ADR-0026) такое
    сочетание означает, что перевозчиком не возил ещё никто на платформе.
    """

    carrier_id: UUID
    carrier_code: str
    carrier_name: str
    score: int | None
    confidence: ScoreConfidence
    #: На чьих данных стоит число: платформенных, своих или обоих (ADR-0026).
    #: Показывается рядом со скором обязательно: оценка платформы и оценка
    #: по своим отправлениям — разные утверждения, и клиент вправе знать,
    #: какое из них перед ним.
    basis: ScoreBasis = ScoreBasis.NONE
    #: Размер платформенной выборки под числом. ``None`` — базы не было.
    #: Числа клиентов здесь нет намеренно: сколько компаний возит этим
    #: перевозчиком — сведение о клиентской базе платформы, а не о нём.
    platform_sample_size: int | None = None
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
