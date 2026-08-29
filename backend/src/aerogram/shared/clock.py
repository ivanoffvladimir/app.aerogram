"""Время. Только timezone-aware UTC.

Наивный datetime в этом проекте — ошибка: он молча ломает арифметику сроков доставки,
которые считаются между событиями от разных перевозчиков в разных часовых поясах.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["ensure_utc", "utcnow"]


def utcnow() -> datetime:
    """Текущий момент в UTC, timezone-aware."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Привести момент к UTC.

    Наивное время трактуется как UTC — перевозчики регулярно отдают время без
    смещения, и единственная безопасная трактовка задокументирована в адаптере.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
