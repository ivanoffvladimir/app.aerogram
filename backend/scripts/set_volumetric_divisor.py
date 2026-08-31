"""Задать делитель объёмного веса перевозчику.

Перевозчик тарифицирует по большему из двух весов: фактическому и объёмному,
Д × Ш × В (см) / делитель (FR-1.2). Делитель — величина **договорная**:
у разных перевозчиков и даже у разных договоров он разный, чаще всего 5000
или 6000. Взять его можно только из договора или тарифов перевозчика,
поэтому значения не зашиты в код и не угадываются.

Цена ошибки: при 6000 вместо 5000 объёмный вес занижается на пятую часть,
и котировка расходится со счётом. Ошибка в порядке величины (500 вместо 5000)
даёт цену в десять раз больше и обнаруживается на первом же расчёте — поэтому
скрипт показывает последствия на трёх посылках до записи и спрашивает
подтверждение.

    uv run python scripts/set_volumetric_divisor.py --carrier cdek --divisor 5000
    uv run python scripts/set_volumetric_divisor.py --list

Значение действует на перевозчика целиком, для всех тенантов: колонка живёт
в платформенном справочнике. Если у тенанта свой договор с другим делителем,
это отдельное решение — см. docs/status.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aerogram.config import get_settings
from aerogram.directories.models import Carrier
from aerogram.shared.money import chargeable_weight

#: Посылки для показа последствий: громоздкая лёгкая, средняя и плотная.
#: Первая — та, на которой делитель виден лучше всего.
SAMPLES: tuple[tuple[str, Decimal, int, int, int], ...] = (
    ("коробка 60×50×40 см, 1 кг", Decimal("1"), 60, 50, 40),
    ("коробка 40×30×25 см, 5 кг", Decimal("5"), 40, 30, 25),
    ("плотная 20×20×20 см, 30 кг", Decimal("30"), 20, 20, 20),
)

#: Правдоподобный диапазон. За его пределами почти наверняка опечатка
#: в порядке величины, и она стоит кратной ошибки в цене.
PLAUSIBLE = range(1000, 10001)


def _engine():  # type: ignore[no-untyped-def]
    settings = get_settings()
    return create_async_engine(str(settings.database_migration_url or settings.database_url))


async def _show_all() -> None:
    engine = _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            carriers = list((await db.execute(select(Carrier).order_by(Carrier.code))).scalars())
    finally:
        await engine.dispose()

    if not carriers:
        print("в справочнике нет ни одного перевозчика")
        return
    print(f"{'код':<12}{'название':<24}{'делитель':>10}")
    for carrier in carriers:
        print(f"{carrier.code:<12}{carrier.name:<24}{carrier.volumetric_divisor:>10}")


def _preview(divisor: int) -> None:
    """Показать, во что превращается делитель на понятных посылках."""
    print(f"\nПри делителе {divisor} перевозчику уйдёт расчётный вес:")
    for label, weight, length, width, height in SAMPLES:
        charged = chargeable_weight(weight, length, width, height, divisor)
        marker = " (по объёму)" if charged > weight else " (по факту)"
        print(f"  {label:<28} → {charged} кг{marker}")


async def _store(carrier_code: str, divisor: int) -> None:
    engine = _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db, db.begin():
            carrier = (
                await db.execute(select(Carrier).where(Carrier.code == carrier_code))
            ).scalar_one_or_none()
            if carrier is None:
                raise SystemExit(f"перевозчика с кодом {carrier_code!r} нет в справочнике")
            was = carrier.volumetric_divisor
            carrier.volumetric_divisor = divisor
    finally:
        await engine.dispose()
    print(f"{carrier_code}: делитель {was} → {divisor}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--carrier", help="код перевозчика из справочника")
    parser.add_argument("--divisor", type=int, help="делитель объёмного веса, например 5000")
    parser.add_argument("--list", action="store_true", help="показать текущие значения")
    parser.add_argument(
        "--yes", action="store_true", help="не спрашивать подтверждения (для автоматизации)"
    )
    args = parser.parse_args()

    if args.list:
        asyncio.run(_show_all())
        return

    if not args.carrier or args.divisor is None:
        raise SystemExit("укажите --carrier и --divisor, либо --list")
    if args.divisor <= 0:
        raise SystemExit("делитель должен быть положительным")

    _preview(args.divisor)
    if args.divisor not in PLAUSIBLE:
        print(
            f"\nВНИМАНИЕ: {args.divisor} вне обычного диапазона "
            f"{PLAUSIBLE.start}–{PLAUSIBLE.stop - 1}. Похоже на опечатку в порядке величины."
        )
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("подтверждение невозможно на неинтерактивном вводе: добавьте --yes")
        if input("\nЗаписать? [y/N]: ").strip().lower() not in ("y", "yes", "д", "да"):
            raise SystemExit("отменено")

    asyncio.run(_store(args.carrier, args.divisor))


if __name__ == "__main__":
    main()
