"""Свод наблюдений по платформе: как считается база, общая для всех клиентов.

Отдельный модуль от ``score``, потому что решает другой вопрос. ``score``
отвечает «как превратить доли в баллы», этот — «какие наблюдения вообще
имеют право попасть в число, которое увидит каждый клиент».

**Собирается обходом тенантов, без новых прав.** Таблица ``tenants``
платформенная и RLS на неё не распространяется (ADR-0015), поэтому фоновая
задача открывает по обычной транзакции на тенанта, складывает счётчики
в приложении и записывает итог. Роль с ``BYPASSRLS`` для этого не нужна,
и заводить её ради свода нельзя: она сняла бы единственную защиту, которую
не обойти забытым ``WHERE``.

**Складываются счётчики, а не доли.** Среднее долей приравняло бы клиента
с тремя отправлениями к клиенту с тремя тысячами, и один неудачный месяц
маленького клиента портил бы оценку перевозчика для всех.

**Порог анонимности — три клиента.** Свод по перевозчику публикуется, только
если им возили не менее ``MIN_PLATFORM_TENANTS`` разных тенантов. При двух
каждый из них вычитает свои числа из агрегата и получает статистику второго
почти точно; при одном агрегат и есть его статистика, выданная остальным, —
ровно та утечка, из-за которой появилась ADR-0017. Порог проверяется
**при записи**, а не при показе: строки, которая указывает на одного клиента,
не должно существовать даже в таблице.

**Денег здесь нет.** В свод входят только показатели качества услуги — срок,
срывы, инциденты, прозрачность трекинга. Тариф не входит ни в каком виде:
он коммерческая тайна клиента, и усреднённый по платформе рассказал бы
соседям об уровне чужих договоров.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from aerogram.intelligence.repository import Observations
from aerogram.intelligence.score import (
    MIN_PLATFORM_SAMPLE,
    MIN_PLATFORM_TENANTS,
    PlatformPrior,
)

__all__ = ["PlatformTotals", "accumulate", "prior_from_totals"]


@dataclass(frozen=True, slots=True)
class PlatformTotals:
    """Суммарные счётчики по одному перевозчику через всех тенантов.

    ``tenants`` — сколько разных клиентов дали хоть одно завершённое
    отправление. Это не количество строк и не количество учётных записей:
    именно оно решает, обезличен ли свод.
    """

    carrier_id: UUID
    tenants: int
    finalized: int
    with_deadline: int
    on_time: int
    broken: int
    with_incident: int
    transparent: int

    @property
    def is_publishable(self) -> bool:
        """Можно ли показывать этот свод клиентам.

        Оба порога сразу: трёх клиентов с одним отправлением каждый
        достаточно для анонимности, но не для утверждения о перевозчике.
        """
        return self.tenants >= MIN_PLATFORM_TENANTS and self.finalized >= MIN_PLATFORM_SAMPLE


def accumulate(per_tenant: list[list[Observations]]) -> list[PlatformTotals]:
    """Наблюдения по тенантам → суммарные счётчики по перевозчикам.

    На входе список списков: по одному списку наблюдений на тенанта, ровно
    в том виде, в каком их отдаёт репозиторий под его RLS. Тенант, у которого
    по перевозчику нет ни одного завершённого отправления, в счётчик клиентов
    этого перевозчика не попадает — иначе порог анонимности набирался бы
    теми, кто им не возил.
    """
    totals: dict[UUID, dict[str, int]] = {}
    for observations in per_tenant:
        for observed in observations:
            if observed.finalized <= 0:
                continue
            row = totals.setdefault(
                observed.carrier_id,
                {
                    "tenants": 0,
                    "finalized": 0,
                    "with_deadline": 0,
                    "on_time": 0,
                    "broken": 0,
                    "with_incident": 0,
                    "transparent": 0,
                },
            )
            row["tenants"] += 1
            row["finalized"] += observed.finalized
            row["with_deadline"] += observed.with_deadline
            row["on_time"] += observed.on_time
            row["broken"] += observed.broken
            row["with_incident"] += observed.with_incident
            row["transparent"] += observed.transparent
    return [
        PlatformTotals(
            carrier_id=carrier_id,
            tenants=row["tenants"],
            finalized=row["finalized"],
            with_deadline=row["with_deadline"],
            on_time=row["on_time"],
            broken=row["broken"],
            with_incident=row["with_incident"],
            transparent=row["transparent"],
        )
        for carrier_id, row in totals.items()
    ]


def prior_from_totals(totals: PlatformTotals) -> PlatformPrior:
    """Свод → база, к которой притягиваются наблюдения тенанта.

    ``price_index`` остаётся серединой шкалы: денег в своде нет вовсе.
    Отсутствующая доля тоже остаётся значением по умолчанию — «не измеряли»
    не равно «плохо», и подставить сюда ноль значило бы наказать перевозчика
    за то, что клиенты не ставили дедлайн. Обратное подставление тоже
    запрещено, и это тонкое место: измеренный ноль обязан остаться нулём —
    см. ``_or_default``.
    """
    defaults = PlatformPrior()
    return PlatformPrior(
        on_time=_or_default(_rate(totals.on_time, totals.with_deadline), defaults.on_time),
        reliability=_or_default(_inverse(totals.broken, totals.finalized), defaults.reliability),
        incident_free=_or_default(
            _inverse(totals.with_incident, totals.finalized), defaults.incident_free
        ),
        price_index=defaults.price_index,
        data_quality=_or_default(
            _rate(totals.transparent, totals.finalized), defaults.data_quality
        ),
    )


def _or_default(measured: Decimal | None, default: Decimal) -> Decimal:
    """Значение или умолчание — по ``None``, а не по истинности.

    ``measured or default`` здесь было бы ошибкой, которая не падает:
    ``Decimal(0)`` ложен, и **измеренный ноль** подменился бы нейтральной
    серединой. Перевозчик, срывающий срок у всех, получил бы 0,5 — то есть
    выглядел бы средним ровно там, где он худший.
    """
    return default if measured is None else measured


def _rate(part: int, whole: int) -> Decimal | None:
    """Доля или ``None``, если делить не на что."""
    if whole <= 0:
        return None
    return (Decimal(part) / Decimal(whole)).quantize(Decimal("0.0001"))


def _inverse(part: int, whole: int) -> Decimal | None:
    """Доля «этого не случилось»: единица минус доля случившегося."""
    rate = _rate(part, whole)
    return None if rate is None else Decimal(1) - rate
