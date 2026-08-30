"""Репозиторий расчёта. Единственное место с SQL в модуле."""

from __future__ import annotations

from datetime import datetime
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

    async def find_reusable(self, request_hash: str, now: datetime) -> RateQuote | None:
        """Живая выдача по тому же запросу — вход для FR-1.6.

        Тенант не указывается в условии намеренно: его ставит RLS, и дублировать
        её здесь значило бы завести второе место, где можно ошибиться.

        Берётся самая свежая: под одним отпечатком их может быть несколько,
        и старая, которой жить осталось минуту, вернула бы клиенту почти
        просроченный ``valid_until``.
        """
        stmt = (
            select(RateQuote)
            .where(RateQuote.hash == request_hash, RateQuote.valid_until > now)
            .order_by(RateQuote.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_quote(self, quote_id: UUID) -> RateQuote | None:
        """Снимок запроса со всеми предложениями — вход для рекомендации."""
        return await self._session.get(RateQuote, quote_id)

    async def promises_by_offer(
        self, offer_ids: list[UUID]
    ) -> dict[UUID, tuple[datetime | None, datetime | None]]:
        """Ожидаемая дата и крайний срок по каждому предложению, одним запросом.

        Нужны списку отправлений: оператору важно не «когда обещали вообще»,
        а «успеваем ли». Выбирается пакетом намеренно — по предложению
        на строку списка получился бы запрос на каждую строку.
        """
        if not offer_ids:
            return {}
        stmt = (
            select(RateOffer.id, RateOffer.eta, RateQuote.deadline)
            .join(RateQuote, RateQuote.id == RateOffer.quote_id)
            .where(RateOffer.id.in_(offer_ids))
        )
        rows = (await self._session.execute(stmt)).all()
        return {row.id: (row.eta, row.deadline) for row in rows}
