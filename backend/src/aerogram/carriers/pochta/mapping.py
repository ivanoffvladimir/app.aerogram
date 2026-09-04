"""Единицы, деньги и продукты Почты России.

Источник — официальная документация API Онлайн-сервиса «Отправка»
(`docs/integrations/sources/pochta/otpravka/`, планка ADR-0020). Машинной
спецификации у расчёта нет: справка — HTML по одной странице на метод,
поэтому каждое поле здесь сверено с текстом страницы, а не с генератором.

Единицы, дословно со страницы `nogroup-rate_calculate.html`:

* «Возвращаемые значения указываются в копейках» — то есть ответ приходит
  сразу в наших минорных единицах, и делить его нельзя;
* `mass` — «Масса отправления в граммах», целое число;
* `dimension.length/width/height` — «(сантиметры)», целые числа;
* `delivery-time.min-days` / `max-days` — «(дни)», целые числа.

**Валюта — рубль**, и это решение человека, а не строка источника: в ответе
Почты нет ни одного поля валюты. Сумма без валюты не существует
(CLAUDE.md §6), поэтому код валюты — константа ``POCHTA_CURRENCY``.

**НДС не угадывается — он вычисляется из самого ответа.** Включает ли
«Плата всего» налог, страница расчёта не говорит, и оба чтения опираются
на источник. За «не включает» — симметрия внутри ответа: составляющие
имеют форму `{rate, vat}`, где `rate` дословно «Тариф без НДС». За
«включает» — именование во всей остальной справке: там, где Почта имеет
в виду сумму без налога, она пишет это в имени поля, и таких пар двадцать
две (`ground-rate-wo-vat` против `ground-rate-with-vat`), а «Плата всего
без НДС» называется `total-rate-wo-vat` в восьми методах поиска заказа.
Поле расчёта суффикса не несёт.

Спорить не нужно: в ответе есть и составляющие, и итог. Если `total-rate`
равен сумме их `rate`, налог начислен сверх; если сумме `rate + vat` —
он уже внутри. Это точное равенство на настоящих числах, а не догадка,
и делает его ``vat_reading``. Ставка нужна там, где составляющих не
пришло: доля налога в сумме, которая его содержит, не может превышать
22/122, и превышение тоже доказывает начисление сверх.

**Ставка НДС — 22 %, решение человека.** Справочник самой Почты её
не знает: он держит исторические коды и дописывает актуальное значение
в скобках — «18 — облагается НДС по ставке 18% (с 01.01.2019 – по ставке
20%)» (`enums-vat.html`). Поэтому ставка живёт здесь одной константой.

**Объявленная ценность — в копейках, и это тоже не догадка.** У самого
`declared-value` единица не названа, но её называет остальной контракт
Почты: «insr-value — Объявленная ценность (копейки)» — так в четырнадцати
методах, от создания заказа до архива. Валюта объявленной ценности —
рубль (решение человека): сумму в другой валюте адаптер не отправляет,
потому что перевозчик прочтёт её как рубли.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any, Final

from aerogram.carriers.base import Place
from aerogram.shared.logging import get_logger
from aerogram.shared.money import Money

__all__ = [
    "DEFAULT_PRODUCTS",
    "GRAMS_IN_KG",
    "POCHTA_CURRENCY",
    "PRODUCTS",
    "RATE_FIELDS",
    "VAT_RATE_PERCENT",
    "PochtaProduct",
    "components_sum",
    "dimension_block",
    "mass_grams",
    "money_from_rate",
    "product_by_code",
    "total_price",
    "vat_reading",
]

log = get_logger(__name__)

#: Валюта расчёта. Ответ Почты кода валюты не содержит вовсе, рубль назван
#: решением человека — см. строку документации модуля. Меняется здесь,
#: а не в пяти местах разбора.
POCHTA_CURRENCY: Final = "RUB"

#: Действующая ставка НДС, в процентах. Задана человеком: справочник Почты
#: её не знает (`enums-vat.html` перечисляет 0, 10, 18/20 и расчётные).
#: Мы её не начисляем — налог приходит от перевозчика; она нужна только
#: чтобы проверить, включает ли «Плата всего» этот налог в себя.
VAT_RATE_PERCENT: Final = Decimal(22)

GRAMS_IN_KG: Final = Decimal(1000)

#: Составляющие цены в ответе расчёта. Каждая объявлена типом «Тариф»
#: и приходит как ``{"rate": …, "vat": …}``. Список полный по странице
#: `nogroup-rate_calculate.html`: по нему сверяется итог, поэтому пропущенное
#: поле здесь сделало бы сверку слепой.
RATE_FIELDS: Final[tuple[str, ...]] = (
    "ground-rate",
    "avia-rate",
    "insurance-rate",
    "inventory-rate",
    "notice-rate",
    "oversize-rate",
    "fragile-rate",
    "completeness-checking-rate",
    "contents-checking-rate",
    "sms-notice-recipient-rate",
    "vsd-rate",
)


@dataclass(frozen=True, slots=True)
class PochtaProduct:
    """Один продукт Почты: сочетание вида, категории и вида транспортировки.

    У Почты нет выдачи «список тарифов одним ответом»: один `POST /1.0/tariff`
    считает ровно одно сочетание и возвращает ровно одну цену. Значит
    рейт-шоппинг по ней — это N запросов, и набор сочетаний обязан быть
    виден в одном месте, а не собираться по коду.

    Матрицы допустимых сочетаний в документации нет: справочники видов РПО,
    категорий и видов транспортировки плоские и независимые. Поэтому набор
    ниже — наше продуктовое решение, а не чтение источника, и он намеренно
    короткий: каждое лишнее сочетание тратит суточную квоту на каждый расчёт.
    """

    code: str
    name: str
    mail_type: str
    mail_category: str
    transport_type: str
    #: Категория, которой заменяется обычная, когда клиент просит страховку:
    #: у Почты «объявленная ценность» выражается именно категорией РПО,
    #: отдельного флага страхования в теле расчёта нет.
    insured_category: str | None = None

    def category_for(self, *, insurance: bool) -> str:
        if insurance and self.insured_category:
            return self.insured_category
        return self.mail_category


#: Продукты, которые платформа предлагает по Почте. Значения `mail-type`,
#: `mail-category` и `transport-type` взяты из официальных справочников
#: (`enums-base-mail-type`, `enums-base-mail-category`,
#: `enums-base-transport-type`); их сочетаемость перевозчиком не
#: документирована и проверяется на стенде.
PRODUCTS: Final[dict[str, PochtaProduct]] = {
    product.code: product
    for product in (
        PochtaProduct(
            code="POSTAL_PARCEL:SURFACE",
            name="Посылка нестандартная, наземная",
            mail_type="POSTAL_PARCEL",
            mail_category="ORDINARY",
            transport_type="SURFACE",
            insured_category="WITH_DECLARED_VALUE",
        ),
        PochtaProduct(
            code="EMS:EXPRESS",
            name="Отправление EMS",
            mail_type="EMS",
            mail_category="ORDINARY",
            transport_type="EXPRESS",
            insured_category="WITH_DECLARED_VALUE",
        ),
    )
}

#: Что считаем, если вызывающий не назвал продуктов. Две штуки, а не все
#: двадцать два вида РПО: суточная квота Почты привязана к токену
#: приложения, и её величина до получения доступов неизвестна.
DEFAULT_PRODUCTS: Final[tuple[str, ...]] = ("POSTAL_PARCEL:SURFACE", "EMS:EXPRESS")


def product_by_code(code: object) -> PochtaProduct | None:
    """Продукт по коду. ``None`` — код неизвестен."""
    if not isinstance(code, str):
        return None
    return PRODUCTS.get(code.strip())


def mass_grams(weight_kg: Decimal) -> int:
    """Килограммы в целые граммы, с округлением ВВЕРХ.

    Вверх, потому что округление вниз занижает тариф: счёт от перевозчика
    придёт по его весу, а не по нашему, и разницу заплатит клиент.
    """
    grams = (weight_kg * GRAMS_IN_KG).to_integral_value(rounding=ROUND_CEILING)
    return max(int(grams), 1)


def dimension_block(place: Place) -> dict[str, int]:
    """Блок ``dimension``: сантиметры целыми, как их объявляет Почта."""
    return {
        "length": max(place.length_cm, 1),
        "width": max(place.width_cm, 1),
        "height": max(place.height_cm, 1),
    }


def money_from_rate(value: object) -> Money | None:
    """Составляющая цены («Тариф») → сумма с НДС.

    Форма у всех составляющих одна: ``{"rate": …, "vat": …}``, где `rate` —
    «Тариф без НДС (коп)», а `vat` — «НДС (коп)» и помечен опциональным.
    Отсутствующий НДС читается как ноль: другого значения у отсутствующей
    надбавки быть не может, но это наше правило, а не строка документации.

    ``None`` — составляющей нет. Это не ноль: ноль означал бы «услуга
    бесплатна», а её просто не считали.
    """
    if not isinstance(value, dict):
        return None
    rate = _as_minor(value.get("rate"))
    if rate is None:
        return None
    vat = _as_minor(value.get("vat")) or 0
    return Money(rate + vat, POCHTA_CURRENCY)


def components_sum(body: dict[str, Any]) -> tuple[int, int]:
    """Суммы составляющих ответа: (плата без НДС, налог).

    Каждая составляющая объявлена как «Тариф» и имеет форму
    ``{"rate": …, "vat": …}``, где `rate` дословно «Тариф без НДС (коп)».
    Отсутствующие составляющие просто не участвуют: их не считали.
    """
    rate_total = 0
    vat_total = 0
    for field in RATE_FIELDS:
        block = body.get(field)
        if not isinstance(block, dict):
            continue
        rate = _as_minor(block.get("rate"))
        if rate is None:
            continue
        rate_total += rate
        vat_total += _as_minor(block.get("vat")) or 0
    return rate_total, vat_total


def vat_reading(body: dict[str, Any]) -> str:
    """Включает ли «Плата всего» налог: ``included``, ``excluded`` или ``inconclusive``.

    **Сначала арифметика по составляющим.** В ответе есть и они, и итог,
    поэтому трактовку не нужно выбирать: если `total-rate` в точности равен
    сумме их `rate`, налог начислен сверх; если сумме `rate + vat` — он уже
    внутри. Точное равенство, без допусков: приблизительное совпадение
    означало бы, что мы чего-то не понимаем в ответе, и лучше сказать
    «не знаю», чем округлить до удобного вывода.

    **Потом ставка** — на случай, когда составляющих не пришло. Доля налога
    в сумме, которая его уже содержит, не может превысить 22/122; превышение
    доказывает начисление сверх. Обратного вывода ставка не даёт: услуга,
    освобождённая от НДС, даёт ту же картину, что и сумма с налогом внутри.

    ``inconclusive`` — ответ не различает случаи. Тогда решает умолчание
    вызывающего, а не догадка этой функции.
    """
    rate_minor = _as_minor(body.get("total-rate"))
    if rate_minor is None:
        return "inconclusive"
    vat_minor = _as_minor(body.get("total-vat")) or 0

    parts_rate, parts_vat = components_sum(body)
    if parts_rate > 0 and parts_vat > 0:
        # Обе проверки, а не первая подошедшая: при parts_vat = 0 они
        # совпали бы, и любой ответ «доказывал» бы что угодно.
        if rate_minor == parts_rate:
            return "excluded"
        if rate_minor == parts_rate + parts_vat:
            return "included"

    if (
        vat_minor > 0
        and Decimal(vat_minor) * (Decimal(100) + VAT_RATE_PERCENT)
        > Decimal(rate_minor) * VAT_RATE_PERCENT
    ):
        return "excluded"
    return "inconclusive"


def total_price(body: dict[str, Any]) -> Money | None:
    """Итог к оплате. ``None`` — ответ без ``total-rate``, то есть не предложение.

    Единственное место, где решается судьба НДС. Когда ответ сам себя
    объясняет (``vat_reading``), берём его вывод. Когда не объясняет —
    складываем: осторожная сторона выбрана намеренно, при ошибке в неё
    Почта проигрывает сравнение, которое должна была выиграть, а при ошибке
    в другую Decision Engine порекомендовал бы её ошибочно, и это осталось
    бы в неизменяемом снимке решения.
    """
    rate = _as_minor(body.get("total-rate"))
    if rate is None:
        return None
    vat = _as_minor(body.get("total-vat")) or 0
    reading = vat_reading(body)
    log.info("pochta.vat_reading", reading=reading, has_vat=bool(vat))
    if reading == "included":
        return Money(rate, POCHTA_CURRENCY)
    return Money(rate + vat, POCHTA_CURRENCY)


def _as_minor(value: object) -> int | None:
    """Целые копейки из ответа. ``None`` — значения нет или оно не число.

    ``bool`` отсеивается отдельно: в Python он подкласс ``int``, и `True`
    молча превратился бы в одну копейку.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None
