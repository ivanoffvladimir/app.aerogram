"""Пригодность адреса к перевозке — общее правило для всех путей ввода.

Функция живёт в ``shared``, а не в ``directories``, потому что вопрос
«для чего годится этот адрес» встаёт на двух независимых путях:

* адрес пришёл из стандартизации ДаData (``directories``);
* адрес введён руками в адресной книге (``core``).

Правило одно, и держать его в двух местах значит рано или поздно получить
два разных ответа на один и тот же адрес. Прямой импорт между этими модулями
запрещён контрактом ``core-below-domain``.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["AddressFitness", "FitnessBlocker", "assess_fitness"]


class AddressFitness(StrEnum):
    """Пригодность адреса к перевозке.

    Единой проверки «адрес валиден» недостаточно: для расчёта хватает города,
    для доставки до пункта выдачи улица и дом получателя не нужны вовсе —
    их подставляет перевозчик, а для доставки до двери дом обязателен.
    Одна общая проверка либо отсекла бы половину заказов до пункта выдачи,
    либо пропустила бы в доставку до двери адрес без дома, то есть возврат
    за наш счёт.
    """

    #: Известен дом — годится для доставки до двери.
    DOOR = "door"
    #: Известен только населённый пункт — годится для расчёта и доставки до ПВЗ.
    LOCALITY = "locality"
    #: Не годится ни для чего: не определён населённый пункт.
    UNUSABLE = "unusable"


class FitnessBlocker(StrEnum):
    """Причина, по которой адрес не пригоден. Показывается пользователю."""

    NO_CITY = "no_city"
    FOREIGN_COUNTRY = "foreign_country"
    NO_HOUSE = "no_house"
    POSTAL_BOX = "postal_box"
    LOW_CONFIDENCE = "low_confidence"


def assess_fitness(
    *,
    city_known: bool,
    house_known: bool,
    postal_box: bool = False,
    foreign: bool = False,
) -> tuple[AddressFitness, list[FitnessBlocker]]:
    """Оценить пригодность по трём фактам об адресе.

    Наличие дома и уровень объекта ФИАС — РАЗНЫЕ величины, и путать их нельзя:
    оператор выбирает город из подсказки и дописывает дом руками, и такой
    адрес пригоден для доставки до двери.
    """
    if foreign:
        return AddressFitness.UNUSABLE, [FitnessBlocker.FOREIGN_COUNTRY]
    if not city_known:
        return AddressFitness.UNUSABLE, [FitnessBlocker.NO_CITY]
    # Абонентский ящик — не адрес курьерской доставки ни при каком качестве.
    if postal_box:
        return AddressFitness.LOCALITY, [FitnessBlocker.POSTAL_BOX]
    if not house_known:
        return AddressFitness.LOCALITY, [FitnessBlocker.NO_HOUSE]
    return AddressFitness.DOOR, []
