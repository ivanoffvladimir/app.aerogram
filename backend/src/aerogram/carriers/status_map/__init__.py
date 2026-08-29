"""Карты соответствия статусов ТК → нормализованная модель.

Карты лежат в YAML, а не в коде (раздел 8.2 ТЗ, п. 3): статусы перевозчика меняются
чаще, чем код, и правка YAML не требует ревью логики.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, cast

import yaml

from aerogram.shared.enums import ShipmentStatus

__all__ = ["StatusMap", "load_status_map", "normalize_status"]

_DIR = Path(__file__).parent


@dataclass(frozen=True, slots=True)
class StatusMap:
    """Карта статусов одного перевозчика."""

    carrier_code: str
    by_code: dict[str, ShipmentStatus]

    def normalize(self, status_raw: str) -> tuple[ShipmentStatus, bool]:
        """Привести сырой статус к нормализованному.

        Возвращает пару «статус, не сопоставлен». Несопоставленный статус пишется как
        есть, отправление получает ``IN_TRANSIT`` и попадает в очередь ручного
        сопоставления в админке (раздел 9 ТЗ) — но никогда не роняет обработку события.
        """
        key = status_raw.strip().upper()
        mapped = self.by_code.get(key)
        if mapped is None:
            return ShipmentStatus.IN_TRANSIT, True
        return mapped, False


@cache
def load_status_map(carrier_code: str) -> StatusMap:
    """Загрузить карту статусов перевозчика из YAML."""
    path = _DIR / f"{carrier_code}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"нет карты статусов для перевозчика {carrier_code!r}: {path}")

    raw = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))
    by_code: dict[str, ShipmentStatus] = {}
    for normalized, codes in raw["statuses"].items():
        status = ShipmentStatus(normalized)
        for code in codes or []:
            key = str(code).strip().upper()
            if key in by_code:
                raise ValueError(
                    f"{carrier_code}: статус {key!r} сопоставлен дважды "
                    f"({by_code[key].value} и {status.value})"
                )
            by_code[key] = status
    return StatusMap(carrier_code=carrier_code, by_code=by_code)


def normalize_status(carrier_code: str, status_raw: str) -> tuple[ShipmentStatus, bool]:
    """Короткая форма: нормализовать статус перевозчика."""
    return load_status_map(carrier_code).normalize(status_raw)
