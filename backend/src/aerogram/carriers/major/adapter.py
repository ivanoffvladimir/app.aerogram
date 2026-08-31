"""Адаптер Major Express — заглушка до появления WSDL.

Каждый метод отказывает одинаково и по делу: ``CarrierNotConfigured``
превращается в строку ``failures`` выдачи, а не в 500 и не в пустой ответ.
Отличать «не настроено» от «временно недоступен» важно на экране: во втором
случае оператору предлагают повторить, в первом повторять бесполезно.

Заглушка **не регистрируется** в реестре: зарегистрированный адаптер попадает
в каждый расчёт тенанта, у которого есть учётная запись, и добавлял бы в выдачу
строку отказа при каждом обращении. Регистрация — последний шаг настройки,
см. ``docs/integrations/major-express.md``.
"""

from __future__ import annotations

from aerogram.carriers.base import (
    CancelResult,
    Capabilities,
    CarrierAccount,
    LabelResult,
    Quote,
    QuoteRequest,
    RawEvent,
    RefCatalog,
    ShipmentRequest,
    ShipmentResult,
    WebhookUpdate,
)
from aerogram.carriers.major.client import CARRIER_CODE, MajorExpressClient
from aerogram.shared.enums import LabelFormat
from aerogram.shared.errors import CarrierNotConfigured

__all__ = ["MajorExpressAdapter"]


class MajorExpressAdapter:
    """Major Express. Возможности заявлены по матрице адаптеров ТЗ v3.

    Возможности перечислены заранее намеренно: они берутся из документации
    перевозчика, а не из состояния нашей реализации, и кабинет уже сейчас
    показывает, чего от этого ТК ждать. Сверить их с WSDL — обязательный шаг
    при подключении.
    """

    code = CARRIER_CODE
    name = "Major Express"
    capabilities = Capabilities(
        supports_cancel=True,
        supports_insurance=True,
        supports_terminals=True,
        supported_label_formats=(LabelFormat.PDF_A4,),
    )

    def __init__(self, *, endpoint_url: str | None = None) -> None:
        self._endpoint_url = endpoint_url

    def _client(self, account: CarrierAccount) -> MajorExpressClient:
        return MajorExpressClient(account.credentials, endpoint_url=self._endpoint_url)

    def _refuse(self, account: CarrierAccount, operation: str) -> CarrierNotConfigured:
        """Отказ с причиной: чего именно не хватает — WSDL или учётных данных."""
        try:
            self._client(account).ensure_ready()
        except CarrierNotConfigured as exc:
            return exc
        # WSDL и доступы на месте, а метод ещё не написан. Это тоже «не настроено»,
        # но причина другая, и молчать о ней нельзя.
        return CarrierNotConfigured(
            f"Метод {operation} Major Express ещё не реализован",
            carrier_code=CARRIER_CODE,
        )

    async def quote(self, req: QuoteRequest, acc: CarrierAccount) -> list[Quote]:
        raise self._refuse(acc, "расчёта")

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult:
        raise self._refuse(acc, "создания заказа")

    async def label(self, ext_id: str, fmt: LabelFormat, acc: CarrierAccount) -> LabelResult:
        raise self._refuse(acc, "печати этикетки")

    async def track(self, ext_id: str, acc: CarrierAccount) -> list[RawEvent]:
        raise self._refuse(acc, "трекинга")

    async def cancel(self, ext_id: str, acc: CarrierAccount) -> CancelResult:
        raise self._refuse(acc, "отмены")

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        """Сверка «призраков» (FR-2.5).

        Метод обязан появиться раньше, чем ``create`` уйдёт в бой: без него
        потерянный ответ на создание оставит у перевозчика заказ, о котором
        мы не знаем.
        """
        raise self._refuse(acc, "поиска заказа по номеру")

    async def fetch_refs(self, acc: CarrierAccount) -> RefCatalog:
        raise self._refuse(acc, "выгрузки справочников")

    def parse_webhook(self, payload: dict[str, object]) -> list[WebhookUpdate]:
        """Разбор вебхука. Учётной записи здесь нет, поэтому причина общая."""
        raise CarrierNotConfigured(
            "Приём вебхуков Major Express не настроен", carrier_code=CARRIER_CODE
        )

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        """Проверка подписи вебхука (FR-3.1).

        Отказ, а не ``False``: ``False`` означает «подпись не сошлась», то есть
        обвинение отправителю. Пока проверять нечем, это разные вещи, и путать
        их нельзя — иначе настоящая проблема настройки будет выглядеть
        как попытка подделки.
        """
        raise CarrierNotConfigured(
            "Проверка подписи вебхука Major Express не настроена", carrier_code=CARRIER_CODE
        )
