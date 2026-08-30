"""Состав доступов перевозчика: что просить у клиента с собственным договором.

Сами значения здесь не хранятся и храниться не могут. Они у каждого тенанта
свои, лежат зашифрованными в ``carrier_accounts.credentials_encrypted``
(ADR-0005) и попадают в адаптер уже расшифрованными. Здесь — только
**описание полей**: что спросить, как подписать в кабинете и что из этого
секрет, который нельзя ни показать обратно, ни записать в лог.

Одного набора полей на всех не существует: Major Express — Basic Auth
(логин и пароль), СДЭК — пара ``client_id``/``client_secret`` для OAuth,
у других ТК встречается одиночный ключ API. Поэтому хранилище остаётся
свободным словарём, а состав объявляется отдельно для каждого кода.

Состав объявляется **только для тех ТК, чей протокол уже прочитан по коду
или документации**. Назвать поле раньше, чем известен способ авторизации,
значит попросить у клиента не то и потом просить заново — а «потом» здесь
означает переписку с его бухгалтерией.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CREDENTIAL_SCHEMAS",
    "PENDING_CARRIERS",
    "CredentialField",
    "CredentialSchema",
    "missing_fields",
    "schema_for",
]


@dataclass(frozen=True, slots=True)
class CredentialField:
    """Одно поле доступа."""

    name: str
    #: Подпись для кабинета. Интерфейс русский (CLAUDE.md §6).
    label: str
    #: Секрет: не отображается при вводе, не возвращается в ответах API,
    #: не пишется в логи. Идентификатор клиента секретом не является —
    #: скрывать его значит мешать оператору проверить, что он ввёл.
    secret: bool = True


@dataclass(frozen=True, slots=True)
class CredentialSchema:
    """Что нужно, чтобы работать по договору клиента с этим перевозчиком."""

    fields: tuple[CredentialField, ...]
    #: Где клиенту взять эти значения. Показывается рядом с формой.
    where_to_get: str

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)


CREDENTIAL_SCHEMAS: dict[str, CredentialSchema] = {
    "major": CredentialSchema(
        fields=(
            CredentialField("login", "Логин веб-сервиса", secret=False),
            CredentialField("password", "Пароль веб-сервиса"),
        ),
        where_to_get="Выдаёт менеджер Major Express вместе с доступом к веб-сервису.",
    ),
    "cdek": CredentialSchema(
        fields=(
            CredentialField("client_id", "Идентификатор клиента", secret=False),
            CredentialField("client_secret", "Секрет клиента"),
        ),
        where_to_get="Личный кабинет СДЭК, раздел интеграции: пара для OAuth.",
    ),
}

#: Перевозчики из пилота, чей адаптер ещё не написан. Состав доступов
#: появится вместе с ним, в том же коммите: у ПЭК и Деловых Линий это
#: не обязательно пара «логин-пароль», а домены обоих закрыты сетевой
#: политикой окружения разработки — проверить нечем.
PENDING_CARRIERS: frozenset[str] = frozenset({"dellin", "pecom", "yandex"})


def schema_for(carrier_code: str) -> CredentialSchema | None:
    """Состав доступов перевозчика. ``None`` — состав ещё не определён."""
    return CREDENTIAL_SCHEMAS.get(carrier_code)


def missing_fields(carrier_code: str, credentials: dict[str, str]) -> list[str]:
    """Каких обязательных полей не хватает.

    Пустой список у неизвестного перевозчика — не «всё в порядке», а «нечем
    проверить»: требовать поля, состав которых мы не знаем, было бы вредно.
    """
    schema = schema_for(carrier_code)
    if schema is None:
        return []
    return [name for name in schema.names if not credentials.get(name)]
