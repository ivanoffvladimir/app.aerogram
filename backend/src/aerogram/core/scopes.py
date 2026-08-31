"""Область действия API-ключа: что машинному клиенту разрешено (FR-10.2).

Таблица одна и проверяется в одном месте — ``core.deps.current_principal``.
Причина именно такая: раздавать проверку по эндпоинтам значит однажды забыть
её на новом, и забытая проверка не видна ни в тестах, ни в ревью — путь просто
работает шире, чем обещано.

**Умолчание — запрет.** Путь, которого нет в таблице, машинному клиенту
недоступен. Новый эндпоинт по умолчанию закрыт для ключей, и открыть его —
осознанное действие, а не следствие забывчивости. Тест
``tests/unit/test_api_scopes.py`` следит, чтобы каждый путь с субъектом был
здесь классифицирован: либо у него есть область, либо он назван кабинетным.

Пользователя кабинета таблица не касается: у человека права определяются
ролью, а не областью ключа. Область сужает машинный доступ, а не заменяет
проверку роли — там, где стоит ``require_roles``, она работает как прежде.
"""

from __future__ import annotations

__all__ = [
    "ALL_SCOPES",
    "API_PREFIX",
    "CABINET_ONLY",
    "MACHINE_SCOPES",
    "SCOPE_LABELS",
    "WITHOUT_SCOPE",
    "required_scope",
]

#: Общий префикс версии API. Живёт здесь, а не в ``main``, потому что
#: сопоставление пути с таблицей его достраивает: маршруты вложенных роутеров
#: знают только свой путь внутри роутера, а префикс FastAPI держит отдельно.
API_PREFIX = "/v1"

#: Путь → область, которую обязан иметь ключ. Путь полный, как в контракте
#: и в ответе OpenAPI, с шаблонами параметров.
MACHINE_SCOPES: dict[tuple[str, str], str] = {
    # Расчёт и ранжирование — одна область: ранжирование работает на уже
    # полученной выдаче и новых данных о тенанте не открывает.
    ("POST", "/v1/rates"): "rates:read",
    ("POST", "/v1/routing/quote"): "rates:read",
    # Решение отделено от расчёта: оно попадает в неизменяемый снимок,
    # на котором строится вся аналитика.
    ("POST", "/v1/decisions"): "decisions:write",
    ("GET", "/v1/shipments"): "shipments:read",
    ("GET", "/v1/shipments/{shipment_id}"): "shipments:read",
    ("GET", "/v1/shipments/{shipment_id}/tracking"): "shipments:read",
    ("GET", "/v1/tracking/exceptions"): "shipments:read",
    # Создание и отмена — одна область: и то и другое меняет заказ
    # у перевозчика и стоит денег.
    ("POST", "/v1/shipments"): "shipments:write",
    ("POST", "/v1/shipments/{shipment_id}/cancel"): "shipments:write",
    ("GET", "/v1/carriers"): "carriers:read",
    ("GET", "/v1/carriers/{code}/terminals"): "carriers:read",
    ("GET", "/v1/analytics/carriers"): "analytics:read",
    ("GET", "/v1/reports/summary"): "analytics:read",
    ("POST", "/v1/addresses/normalize"): "directories:read",
    ("GET", "/v1/cities/suggest"): "directories:read",
    ("POST", "/v1/parties/lookup"): "directories:read",
    ("GET", "/v1/webhooks/subscriptions"): "webhooks:read",
    ("POST", "/v1/webhooks/subscriptions"): "webhooks:write",
    ("DELETE", "/v1/webhooks/subscriptions/{subscription_id}"): "webhooks:write",
}

#: Пути, доступные ключу без всякой области: они не отдают данных тенанта
#: и нужны самому клиенту, чтобы понять, от чьего имени он ходит.
WITHOUT_SCOPE: frozenset[tuple[str, str]] = frozenset({("GET", "/v1/auth/me")})

#: Пути кабинета: машинному клиенту закрыты целиком, области для них нет.
#:
#: Управление пользователями и ключами закрыто ради того, чтобы украденный
#: ключ нельзя было превратить в постоянный доступ: выпустить себе второй
#: ключ, завести пользователя или снять второй фактор он не может.
#: Адресная книга закрыта по другой причине — в ТЗ v3 её нет вовсе, и открывать
#: машинному клиенту то, о чём никто не договаривался, не за чем: адреса
#: интеграция передаёт прямо в запросе.
CABINET_ONLY: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/v1/auth/mfa/setup"),
        ("POST", "/v1/auth/mfa/enable"),
        ("POST", "/v1/auth/mfa/disable"),
        ("GET", "/v1/users"),
        ("POST", "/v1/users"),
        ("GET", "/v1/api-keys"),
        ("POST", "/v1/api-keys"),
        ("DELETE", "/v1/api-keys/{key_id}"),
        ("GET", "/v1/counterparties"),
        ("POST", "/v1/counterparties"),
        ("GET", "/v1/counterparties/{counterparty_id}"),
        ("DELETE", "/v1/counterparties/{counterparty_id}"),
        ("GET", "/v1/counterparties/{counterparty_id}/addresses"),
        ("POST", "/v1/counterparties/{counterparty_id}/addresses"),
        ("GET", "/v1/admin/city-mappings"),
        ("POST", "/v1/admin/city-mappings/{item_id}/confirm"),
    }
)

#: Все области, которые можно выдать ключу. Порядок — как в кабинете.
ALL_SCOPES: tuple[str, ...] = (
    "rates:read",
    "decisions:write",
    "shipments:read",
    "shipments:write",
    "carriers:read",
    "analytics:read",
    "directories:read",
    "webhooks:read",
    "webhooks:write",
)

#: Подписи для кабинета и скриптов. Русские: их читает человек.
SCOPE_LABELS: dict[str, str] = {
    "rates:read": "Расчёт и ранжирование",
    "decisions:write": "Фиксация решений",
    "shipments:read": "Чтение отправлений и трекинга",
    "shipments:write": "Создание и отмена отправлений",
    "carriers:read": "Перевозчики и терминалы",
    "analytics:read": "Аналитика и сводка",
    "directories:read": "Справочники: адреса, города, контрагенты",
    "webhooks:read": "Чтение подписок на события",
    "webhooks:write": "Управление подписками на события",
}


def route_key(method: str, path_format: str) -> tuple[str, str]:
    """Ключ таблицы по методу и пути маршрута.

    Путь маршрута вложенного роутера не содержит префикса версии — FastAPI
    хранит префикс рядом, а не в самом маршруте, — поэтому он достраивается.
    Проверка ``startswith`` оставлена ради маршрутов, объявленных прямо
    на приложении: у них путь уже полный.
    """
    full = path_format if path_format.startswith(API_PREFIX) else API_PREFIX + path_format
    return method.upper(), full


def required_scope(method: str, path: str) -> str | None:
    """Какая область нужна для пути, или ``None``, если он открыт без неё.

    Вызывающий обязан отличать ``None`` от отсутствия пути в таблице:
    ``None`` означает «область не нужна», а отсутствие — «запрещено».
    Поэтому наличие проверяется отдельно, через ``MACHINE_SCOPES``
    и ``WITHOUT_SCOPE``, а не по возвращённому значению.
    """
    key = (method.upper(), path)
    if key in WITHOUT_SCOPE:
        return None
    return MACHINE_SCOPES.get(key)
