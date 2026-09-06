"""Проверка доступов перевозчика: замер, разбор отказа, безопасный текст.

Один помощник на все адаптеры. Каждый из них знает, **какой** вызов дешевле
всего подтверждает доступы, но что делать с исключением и как померить время —
одинаково у всех, и пять копий этой логики разошлись бы по мелочам: где-то
таймаут стал бы «недоступен», где-то «неверные доступы».

Три правила живут здесь.

**Отказ перевозчика — результат, а не исключение.** Метод отвечает на вопрос
«работают ли доступы», и «не работают» такой же законный ответ, как
«работают». Поднять исключение значило бы заставить вызывающего ловить его
и превращать обратно в ответ, а по дороге ошибка перевозчика однажды дала бы
500 (CLAUDE.md §6).

**Текст отказа наш, а не перевозчика.** В `status_message` попадает
`message_ru` нашей иерархии ошибок — «Перевозчик отклонил учётные данные»,
«Перевозчик не ответил за отведённое время». Тело ответа перевозчика туда
не переписывается: оно может содержать эхо логина или адреса, а поле
показывается в кабинете и живёт в базе. На всякий случай текст ещё и
подрезается: длина сообщения — не то, ради чего существует проверка.

**Неизвестное исключение не выдаётся за отказ перевозчика.** Ошибка в нашем
коде — это наша ошибка, и в кабинете она обязана называться иначе, иначе
клиент пойдёт перевыпускать доступы, с которыми всё в порядке.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Final

from aerogram.carriers.base import HealthResult
from aerogram.shared.errors import AerogramError
from aerogram.shared.logging import get_logger

__all__ = ["MESSAGE_LIMIT", "guard", "probe"]

log = get_logger(__name__)

#: Сколько символов отказа хранится и показывается. Всё, что длиннее, —
#: это уже трассировка перевозчика, а не объяснение оператору.
MESSAGE_LIMIT: Final = 200

_UNEXPECTED: Final = "Проверка не выполнена из-за внутренней ошибки платформы"


async def probe(call: Callable[[], Awaitable[object]], *, carrier_code: str) -> HealthResult:
    """Выполнить проверочный вызов и превратить его исход в ``HealthResult``.

    Время меряется вокруг **вызова перевозчика**, а не вокруг всей проверки:
    расшифровка учётных данных и запись итога — наше время, и приписывать его
    перевозчику неверно.
    """
    started = time.monotonic()
    try:
        await call()
    except AerogramError as exc:
        return _failed(started, exc.message_ru, carrier_code=carrier_code, kind=type(exc).__name__)
    except Exception as exc:
        # Тип, а не текст: в сообщении может оказаться шифротекст учётных
        # данных или персональные данные адреса.
        log.error(
            "carrier.health_check_failed",
            carrier=carrier_code,
            error_type=type(exc).__name__,
        )
        return _failed(started, _UNEXPECTED, carrier_code=carrier_code, kind=type(exc).__name__)
    return HealthResult(is_healthy=True, latency_ms=_elapsed_ms(started))


async def guard(call: Callable[[], Awaitable[HealthResult]], *, carrier_code: str) -> HealthResult:
    """Страховка на границе домена: адаптер обязан вернуть результат.

    ``probe`` защищает вызов перевозчика внутри адаптера, а этот — вызов
    самого адаптера. Разница не теоретическая: контракт объявляет отказ
    результатом, но соблюдение контракта проверить нечем, а ошибка внутри
    адаптера (или адаптер, написанный мимо правила) уходила бы клиенту
    пятисотой. Домен не обязан доверять адаптеру — по той же причине,
    по которой расчёт не роняет всю выдачу из-за одного перевозчика.
    """
    started = time.monotonic()
    try:
        return await call()
    except AerogramError as exc:
        return _failed(started, exc.message_ru, carrier_code=carrier_code, kind=type(exc).__name__)
    except Exception as exc:
        log.error(
            "carrier.health_check_failed",
            carrier=carrier_code,
            error_type=type(exc).__name__,
        )
        return _failed(started, _UNEXPECTED, carrier_code=carrier_code, kind=type(exc).__name__)


def _failed(started: float, message: str, *, carrier_code: str, kind: str) -> HealthResult:
    log.info("carrier.health_check_refused", carrier=carrier_code, error_type=kind)
    return HealthResult(
        is_healthy=False,
        latency_ms=_elapsed_ms(started),
        message=message[:MESSAGE_LIMIT],
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
