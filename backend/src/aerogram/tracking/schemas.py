"""DTO трекинга. Соответствуют схеме ``TrackingEvent`` из openapi.yaml."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

__all__ = ["TrackingEventOut"]


class TrackingEventOut(BaseModel):
    """Событие ленты в едином виде, независимо от перевозчика (FR-3.4).

    ``carrier_status`` отдаётся рядом с нормализованным намеренно: когда
    оператор звонит перевозчику, разговаривать он будет на языке перевозчика,
    а не на нашем.
    """

    occurred_at: datetime
    normalized_status: str
    carrier_status: str
    location: str | None = None
    description: str | None = None
