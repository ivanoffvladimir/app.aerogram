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

Три места, где источник молчит, а решение всё равно нужно. Каждое названо
здесь, повторено в ADR-0023 и обязано быть проверено на живом стенде.

**Валюта.** В ответе нет ни одного поля валюты. «Копейки» подразумевают
рубль, но дословно этого не написано, поэтому RUB — наша константа
с комментарием, а не факт источника (CLAUDE.md §6: суммы без валюты
не существует).

**НДС.** `total-rate` описан как «Плата всего (коп)», `total-vat` — «Всего
НДС (коп)», и включает ли первое второе — не сказано. Читаем по симметрии
с составляющими, где `rate` — «Тариф без НДС», а `vat` — «НДС»: итог
к оплате есть сумма обоих. Ошибка здесь стоит 20 % цены, поэтому сложение
живёт в одной функции ``total_price``, а не размазано по разбору.

**Объявленная ценность на входе.** У `declared-value` в таблице написано
только «Целое число», без единицы: фраза про копейки относится к
возвращаемым значениям. Передаём минорные единицы — это единственная
трактовка, согласованная с остальным денежным контрактом Почты.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any, Final

from aerogram.carriers.base import Place
from aerogram.shared.money import Money

__all__ = [
    "DEFAULT_PRODUCTS",
    "GRAMS_IN_KG",
    "POCHTA_CURRENCY",
    "PRODUCTS",
    "PochtaProduct",
    "dimension_block",
    "mass_grams",
    "money_from_rate",
    "product_by_code",
    "total_price",
]

#: Валюта расчёта. Ответ Почты кода валюты не содержит вовсе — см. строку
#: документации модуля. Меняется здесь, а не в пяти местах разбора.
POCHTA_CURRENCY: Final = "RUB"

GRAMS_IN_KG: Final = Decimal(1000)


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


def total_price(body: dict[str, Any]) -> Money | None:
    """Итог к оплате: ``total-rate`` плюс ``total-vat``.

    Единственное место, где эти два поля складываются — см. строку
    документации модуля про НДС. ``None`` — ответ без ``total-rate``,
    то есть не предложение вовсе.
    """
    rate = _as_minor(body.get("total-rate"))
    if rate is None:
        return None
    vat = _as_minor(body.get("total-vat")) or 0
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
