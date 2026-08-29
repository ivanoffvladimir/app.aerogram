"""Реестр адаптеров.

Единственная точка, через которую домен обращается к перевозчикам (CLAUDE.md §4,
контракт ``no-direct-carrier``). Прямой импорт ``carriers.cdek`` из ``rating``,
``shipments`` или ``tracking`` — ошибка CI, а не вкусовщина: он делает добавление
нового ТК изменением в ядре.
"""

from __future__ import annotations

from aerogram.carriers.base import CarrierAdapter

__all__ = ["all_adapters", "available_codes", "get_adapter", "register"]

_REGISTRY: dict[str, CarrierAdapter] = {}


def register(adapter: CarrierAdapter) -> None:
    """Зарегистрировать адаптер. Повторная регистрация того же кода запрещена."""
    if adapter.code in _REGISTRY:
        raise ValueError(f"адаптер с кодом {adapter.code!r} уже зарегистрирован")
    _REGISTRY[adapter.code] = adapter


def get_adapter(code: str) -> CarrierAdapter:
    """Получить адаптер по коду перевозчика."""
    try:
        return _REGISTRY[code]
    except KeyError:
        raise LookupError(f"адаптер перевозчика {code!r} не зарегистрирован") from None


def available_codes() -> tuple[str, ...]:
    """Коды зарегистрированных перевозчиков."""
    return tuple(sorted(_REGISTRY))


def all_adapters() -> tuple[CarrierAdapter, ...]:
    """Все зарегистрированные адаптеры."""
    return tuple(_REGISTRY[code] for code in available_codes())


def _reset_for_tests() -> None:
    """Очистить реестр. Только для тестов."""
    _REGISTRY.clear()
