"""Репозиторий расчёта. Единственное место с SQL в модуле."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.rating.models import RateQuote, RateRequest

__all__ = ["RateRepository"]


class RateRepository:
    """Запросы расчёта и котировки.

    Каждый запрос и каждая котировка сохраняются вместе с сырым ответом ТК
    (FR-1.7): это исходные данные для Carrier Score и для разбора спорных
    ситуаций, и восстановить их потом неоткуда.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add_request(self, request: RateRequest) -> RateRequest:
        self._session.add(request)
        return request

    def add_quotes(self, quotes: list[RateQuote]) -> None:
        self._session.add_all(quotes)

    async def get_quote(self, quote_id: UUID) -> RateQuote | None:
        """Котировка по идентификатору — для создания отправления по rate_id."""
        stmt = select(RateQuote).where(RateQuote.id == quote_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_request(self, request_id: UUID) -> RateRequest | None:
        return await self._session.get(RateRequest, request_id)
