"""Расписание Celery и реестр задач не должны расходиться.

Beat ставит в очередь ИМЯ задачи. Если задачи с таким именем в воркере нет,
он отвечает `NotRegistered` — в журнале воркера, куда никто не смотрит, —
а расписание при этом выглядит так, будто функция работает. Именно так
`sync_carrier_references` и `purge_raw_calls` числились рабочими, не существуя.

Приём тот же, что уже принят для путей контракта в
``tests/integration/test_openapi_conformance.py``: ещё не написанное
перечисляется явно, чтобы список сокращался осознанно, а не забывался.
"""

from __future__ import annotations

import pytest

from aerogram.worker import tasks as _tasks  # noqa: F401  — регистрирует задачи
from aerogram.worker.app import app

#: Задачи, объявленные в расписании и ещё не написанные. Список обязан
#: сокращаться. Пустой означает, что расписание закрыто целиком.
NOT_WRITTEN_YET: frozenset[str] = frozenset(
    {
        # Список отправлений с риском срыва срока. ТЗ v3, раздел 10, называет
        # признак («отсутствие обновлений сверх порога»), но не говорит, что
        # именно показывать и кому. Требует решения человека — см. docs/status.md.
        "aerogram.worker.tasks.detect_delivery_risk",
        # Ежесуточная сводка тенанту: не решено, куда она уходит (почта,
        # Telegram, вебхук) и что в неё входит.
        "aerogram.worker.tasks.send_daily_digest",
    }
)


def scheduled() -> set[str]:
    return {entry["task"] for entry in app.conf.beat_schedule.values()}


def registered() -> set[str]:
    return {name for name in app.tasks if name.startswith("aerogram.")}


class TestSchedule:
    def test_every_scheduled_task_exists_or_is_declared_missing(self) -> None:
        """Иначе beat молча зовёт имя, которого нет."""
        missing = sorted(scheduled() - registered() - NOT_WRITTEN_YET)
        assert not missing, f"в расписании есть, а задачи нет: {missing}"

    def test_the_missing_list_is_honest(self) -> None:
        """Написанная задача не должна оставаться в списке ненаписанных."""
        stale = sorted(NOT_WRITTEN_YET & registered())
        assert not stale, f"уже написано, убрать из NOT_WRITTEN_YET: {stale}"

    def test_no_task_is_written_and_never_scheduled(self) -> None:
        """Задача, которую никто не ставит в очередь, не выполняется никогда.

        Если появится задача, запускаемая только по событию, её придётся
        назвать здесь так же явно, как ненаписанные, — молчаливого исключения
        быть не должно.
        """
        orphans = sorted(registered() - scheduled())
        assert not orphans, f"задача написана, но не запланирована: {orphans}"

    @pytest.mark.parametrize("name", sorted(NOT_WRITTEN_YET))
    def test_a_declared_missing_task_is_actually_scheduled(self, name: str) -> None:
        """Список ненаписанных — про расписание, а не свалка любых имён."""
        assert name in scheduled(), f"нет в расписании, убрать из списка: {name}"
