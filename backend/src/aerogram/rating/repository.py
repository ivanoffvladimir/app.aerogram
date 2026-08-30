"""Репозиторий расчёта. Единственное место с SQL в модуле."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.rating.models import RateOffer, RateQuote

__all__ = ["RateRepository"]


class RateRepository:
    """Снимки запросов расчёта и предложения перевозчиков.

    Каждый запрос и каждое предложение сохраняются вместе с сырым ответом ТК:
    это исходные данные для Carrier Score и для разбора спорных ситуаций,
    и восстановить их потом неоткуда.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add_quote(self, quote: RateQuote) -> RateQuote:
        """Снимок запроса расчёта со всеми его входными данными."""
        self._session.add(quote)
        return quote

    def add_offers(self, offers: list[RateOffer]) -> None:
        self._session.add_all(offers)

    async def get_offer(self, offer_id: UUID) -> RateOffer | None:
        """Предложение по идентификатору — для создания отправления по нему."""
        stmt = select(RateOffer).where(RateOffer.id == offer_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_quote(self, quote_id: UUID) -> RateQuote | None:
        """Снимок запроса со всеми предложениями — вход для рекомендации."""
        return await self._session.get(RateQuote, quote_id)
