"""Адаптер Почты России.

Расчёт, оформление, сверка «призраков» и печатная форма Ф7п. Трекинг
и отмена не реализованы, и каждый из них честно отказывает с причиной,
а не молчит: причины разные и обе не в нашей власти.

Написан по **официальной документации перевозчика** — планка ADR-0020.
Машинная спецификация у Почты есть только на трекинг (два WSDL, которые она
раздаёт сама), а расчёт и оформление описаны прозой: 117 страниц справки
API Онлайн-сервиса «Отправка», по одной на метод. Все они выкачаны
в `docs/integrations/sources/pochta/`, и каждое поле сверено с текстом.

Решения, следующие прямо из источника.

**Боевого адреса API в документации нет.** Все страницы дают только
«Локальный URL» вида `/1.0/tariff`; полный адрес встречается ровно в одном
файле примеров. Поэтому боевой адрес задаётся в учётной записи тенанта,
а его отсутствие — отказ, а не подстановка выдуманного хоста (см. `client`).

**Один запрос — одна цена.** Выдачи списком у Почты нет: расчёт считает одно
сочетание вида РПО, категории и вида транспортировки. Рейт-шоппинг — это N
запросов, и набор продуктов объявлен явно, потому что каждый лишний тратит
суточную квоту (см. `mapping`).

**ШПИ выдаёт уже создание заказа — но только версии 2.0.** `PUT
/2.0/user/backlog` возвращает `orders[].barcode` — «ШПИ отправления», тогда
как версия 1.0 отдаёт лишь внутренние идентификаторы, и справка сама зовёт
её «Создание заказа без ШПИ».

**Стадия 2 — оформление, сверка и печатная форма.** Заказ создаётся,
ищется по нашему же номеру (`order-num` → `GET /1.0/backlog/search`,
сверка «призраков» FR-2.5) и печатается формой Ф7п. Партия, сессия и сдача
в отделение остаются за кадром: это уже документооборот отправителя,
а не создание отправления, и наш контракт его не описывает.

**Оформлению нужен разобранный адрес, а домен его не носит.** Почте
обязательны `region-to`, `street-to`, `house-to` и фамилия с именем
получателя порознь, тогда как `Party` несёт адрес одной строкой и ФИО
не несёт вовсе. Разбирать строку регулярным выражением нельзя: ошибка
разбора отправляет груз к другому дому, и молча. Поэтому части берутся
из `extras`, а без них оформление отказывает до вызова перевозчика —
и сегодня отказывает всегда, потому что `extras` домен не заполняет.
Чем донести эти поля, решает человек: это правка `carriers/base.py`
(CLAUDE.md §7, пункт 3).

**Отмены нет вовсе.** Ни в одном из 117 методов нет отмены отправления:
у «Отправки» есть удаление заказа из черновиков до формирования партии,
что нашим синхронным `CancelResult` не описывается. Это и есть
`supports_cancel = False` из решения 4 ADR-0020, подтверждённое чтением.

**Вебхуков нет, и трекинга пока тоже.** Ни приёма события, ни подписки
на него в справке нет: трекинг Почты живёт на опросе и лежит в отдельном
SOAP-сервисе. Подключить его — значит завести `zeep` вне `carriers/major`,
то есть поправить ADR-0013 и добавить зависимость, а это решение человека
(CLAUDE.md §2).
"""

from __future__ import annotations

from typing import Any

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
from aerogram.carriers.pochta.client import BASE_URL_SETTING, PochtaClient, user_key
from aerogram.carriers.pochta.mapping import PochtaProduct, product_by_code
from aerogram.carriers.pochta.orders import (
    BACKLOG_PATH,
    POSTOFFICE_SETTING,
    SEARCH_PATH,
    create_payload,
    form_path,
    parse_created,
    parse_found,
)
from aerogram.carriers.pochta.quotes import TARIFF_PATH, build_tariff_payload, parse_tariff
from aerogram.carriers.pochta.quotes import products_for as _products_for
from aerogram.shared.enums import LabelFormat
from aerogram.shared.errors import (
    CarrierAuthError,
    CarrierError,
    CarrierNotConfigured,
    CarrierRateLimited,
    CarrierTimeout,
    CarrierUnavailable,
    CarrierValidationError,
)
from aerogram.shared.logging import get_logger

__all__ = ["POCHTA_CODE", "PochtaAdapter"]

log = get_logger(__name__)

POCHTA_CODE = "pochta"


class PochtaAdapter:
    """Реализация ``CarrierAdapter`` для Почты России."""

    code = POCHTA_CODE
    name = "Почта России"
    capabilities = Capabilities(
        # Ни приёма события, ни подписки в справке «Отправки» нет —
        # см. строку документации модуля.
        supports_webhooks=False,
        # Отметка «Курьер» есть в теле расчёта и заказа, а в справочниках —
        # статусы заявки на вызов курьера. Но самого метода подачи такой
        # заявки нет ни на одной из 59 страниц с методами: подать её нечем,
        # значит и обещать нечего.
        supports_pickup_request=False,
        # Наложенный платёж выражается категорией РПО, но поля для его суммы
        # в теле расчёта нет ни одного: обещать нечем.
        supports_cod=False,
        # Объявленная ценность — категория РПО плюс `declared-value`.
        supports_insurance=True,
        supports_cancel=False,
        supports_terminals=False,
        # Объёмного веса у Почты нет: тариф считается от массы, а за габариты
        # начисляется отдельная надбавка за негабарит. Досчитывать объёмный
        # вес на нашей стороне значило бы отправить перевозчику массу,
        # которой не существует, и получить цену чужого отправления (FR-1.2).
        computes_volumetric_weight=True,
        # Одно РПО — одно место: в теле расчёта одна `mass` и один `dimension`.
        max_places=1,
        # Ф7п приходит готовым PDF-ом: «Генерирует и возвращает pdf файл
        # с формой ф7п для указанного заказа». Размер листа перевозчик
        # не называет, а форма 7п — бланк сопроводительного адреса на A4;
        # других форматов у метода нет.
        supported_label_formats=(LabelFormat.PDF_A4,),
    )

    def __init__(self, client_factory: Any = None) -> None:
        """``client_factory`` подменяется в тестах; в проде клиент строится сам."""
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client(acc: CarrierAccount) -> PochtaClient:
        credentials = acc.credentials
        token = credentials.get("token") or ""
        if not token:
            raise CarrierValidationError(
                "В учётной записи Почты России не задан токен авторизации приложения",
                carrier_code=POCHTA_CODE,
            )
        base_url = acc.settings.get(BASE_URL_SETTING)
        return PochtaClient(
            token=token,
            user_auth_key=user_key(
                login=credentials.get("login"),
                password=credentials.get("password"),
                key=credentials.get("user_key"),
            ),
            base_url=base_url if isinstance(base_url, str) else None,
            is_sandbox=acc.is_sandbox,
        )

    # --- Расчёт -----------------------------------------------------------

    async def quote(self, req: QuoteRequest, acc: CarrierAccount) -> list[Quote]:
        """Расчёт по каждому запрошенному продукту.

        Продукт, который перевозчик считать отказался, гасит себя, а не всю
        выдачу по Почте: сочетаемость видов РПО с категориями и видами
        транспортировки нигде не документирована, и отказ по одному
        сочетанию — ожидаемый исход, а не сбой.

        Гасится **только отказ по этому сочетанию**. Всё, что относится
        к запросу или к учётной записи целиком — нет индекса, несколько мест,
        неверный токен, таймаут, разомкнутый предохранитель, — поднимается
        сразу: повторять это по каждому продукту значит тратить суточную
        квоту на заведомо тот же ответ.

        Если не уцелело ни одного предложения, последний отказ поднимается
        наружу. Молча вернуть пустой список значило бы сказать «Почта
        не возит по этому направлению» там, где она сказала почему.
        """
        client = self._client_factory(acc)
        try:
            quotes: list[Quote] = []
            last_refusal: CarrierError | None = None
            for product in _products_for(req):
                try:
                    quote = await self._quote_one(client, req, acc, product)
                except (
                    CarrierValidationError,
                    CarrierAuthError,
                    CarrierTimeout,
                    CarrierUnavailable,
                    CarrierRateLimited,
                ):
                    raise
                except CarrierError as exc:
                    # Не молча: продукт, который перевозчик считать отказался,
                    # должен быть виден в логах, а не выглядеть как «тарифов
                    # у Почты стало меньше».
                    log.info("pochta.product_refused", product=product.code, reason=str(exc)[:200])
                    last_refusal = exc
                    continue
                if quote is not None:
                    quotes.append(quote)
            if not quotes and last_refusal is not None:
                raise last_refusal
            return quotes
        finally:
            await client.aclose()

    async def _quote_one(
        self,
        client: PochtaClient,
        req: QuoteRequest,
        acc: CarrierAccount,
        product: PochtaProduct,
    ) -> Quote | None:
        payload = build_tariff_payload(req, product)
        body = await client.post(TARIFF_PATH, payload, operation="quote")
        return parse_tariff(body, product, price_source=acc.price_source)

    # --- Оформление -------------------------------------------------------

    async def create(self, req: ShipmentRequest, acc: CarrierAccount) -> ShipmentResult:
        """``PUT /2.0/user/backlog``: заказ и сразу ШПИ.

        Продукт берётся из ``tariff_code`` выбранного предложения: у Почты
        это сочетание вида РПО, категории и вида транспортировки, и оформить
        нужно ровно то, что посчитали. Неизвестный код — отказ, а не
        подстановка первого попавшегося продукта: цена показана за один
        продукт, а уехал бы другой.
        """
        product = product_by_code(req.tariff_code) or product_by_code(req.service_code)
        if product is None:
            raise CarrierValidationError(
                f"Неизвестный продукт Почты России: «{req.tariff_code or req.service_code}»",
                field="tariff_code",
                carrier_code=POCHTA_CODE,
            )
        sender_index = acc.settings.get(POSTOFFICE_SETTING)
        payload = create_payload(
            req,
            product,
            sender_index=sender_index if isinstance(sender_index, str) else None,
        )
        client = self._client_factory(acc)
        try:
            body = await client.put(BACKLOG_PATH, payload, operation="create")
        finally:
            await client.aclose()
        if not isinstance(body, dict):
            raise CarrierError(
                "Почта России ответила на создание заказа неожиданным телом",
                carrier_code=POCHTA_CODE,
            )
        return parse_created(body, number=req.number)

    async def find_by_number(self, number: str, acc: CarrierAccount) -> ShipmentResult | None:
        """``GET /1.0/backlog/search?query=`` — сверка «призраков» (FR-2.5).

        Ищет по ``order-num``, куда при создании ушёл наш собственный номер.
        ``None`` означает «заказа с таким номером у перевозчика нет» и
        только это: ошибка вызова остаётся ошибкой и наверх поднимается.
        Принять сбой за «не найден» значит создать второй заказ — с новым
        ШПИ, вторым грузом и вторым счётом.
        """
        client = self._client_factory(acc)
        try:
            body = await client.get_json(SEARCH_PATH, operation="find", params={"query": number})
        finally:
            await client.aclose()
        return parse_found(body, number=number)

    async def label(self, ext_id: str, fmt: LabelFormat, acc: CarrierAccount) -> LabelResult:
        """``GET /1.0/forms/{id}/f7pdf`` — форма Ф7п готовым PDF-ом.

        Единственный формат: метод отдаёт файл, а выбора листа у него нет.
        Просьбу о другом формате отклоняем вслух — молча подменив формат,
        мы отдали бы этикетку, которую не на что наклеить.
        """
        if fmt is not LabelFormat.PDF_A4:
            raise CarrierValidationError(
                f"Почта России отдаёт форму Ф7п только в PDF, запрошен {fmt.value}",
                field="format",
                carrier_code=POCHTA_CODE,
            )
        client = self._client_factory(acc)
        try:
            content = await client.get_bytes(form_path(ext_id), operation="label")
        finally:
            await client.aclose()
        if not content:
            # Пустой файл — не форма. Отдать его значило бы сказать
            # «печатайте», а печатать нечего.
            raise CarrierError("Почта России вернула пустую форму Ф7п", carrier_code=POCHTA_CODE)
        return LabelResult(format=LabelFormat.PDF_A4, content=content, is_pending=False)

    # --- Ещё не реализовано ----------------------------------------------

    async def track(self, ext_id: str, acc: CarrierAccount) -> list[RawEvent]:
        raise CarrierNotConfigured(
            "Трекинг Почты России ещё не реализован: он живёт в отдельном SOAP-сервисе, "
            "и его подключение требует zeep вне carriers/major, то есть поправки "
            "к ADR-0013 и решения человека",
            carrier_code=POCHTA_CODE,
        )

    async def cancel(self, ext_id: str, acc: CarrierAccount) -> CancelResult:
        raise CarrierNotConfigured(
            "У Почты России нет отмены отправления: до формирования партии заказ "
            "удаляется из черновиков, после — не отменяется вовсе. Синхронное "
            "«да/нет» нашего CancelResult этого не описывает (ADR-0020, решение 4)",
            carrier_code=POCHTA_CODE,
        )

    async def fetch_refs(self, acc: CarrierAccount) -> RefCatalog:
        raise CarrierNotConfigured(
            "Выгрузка справочника отделений Почты России ещё не реализована",
            carrier_code=POCHTA_CODE,
        )

    def parse_webhook(self, payload: dict[str, object]) -> list[WebhookUpdate]:
        """Вебхуков у Почты нет — разбирать нечего.

        Пустой список, а не исключение: приёмник вебхуков не должен падать
        из-за события, пришедшего непонятно от кого. Событие при этом
        не теряется — трекинг у Почты и так работает опросом.
        """
        log.warning("pochta.webhook_unexpected", keys=sorted(payload)[:10])
        return []

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        raise CarrierNotConfigured(
            "Почта России вебхуков не присылает: подтверждать подлинность нечего",
            carrier_code=POCHTA_CODE,
        )
