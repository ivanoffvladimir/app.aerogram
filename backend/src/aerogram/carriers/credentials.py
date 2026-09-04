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
    "WEBHOOK_SECRET_FIELD",
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
    #: Без обязательного поля перевозчик не работает вовсе. Необязательное
    #: включает отдельную возможность: без секрета подписи вебхуков просто
    #: не будет, а расчёт и оформление продолжат работать на опросе.
    required: bool = True


@dataclass(frozen=True, slots=True)
class CredentialSchema:
    """Что нужно, чтобы работать по договору клиента с этим перевозчиком."""

    fields: tuple[CredentialField, ...]
    #: Где клиенту взять эти значения. Показывается рядом с формой.
    where_to_get: str

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields if field.required)


#: Секрет подписи входящих вебхуков. Общий для всех перевозчиков и
#: необязательный: без него приём вебхуков не работает, а расчёт, оформление
#: и трекинг опросом работают. Проверять подпись «как получится» нельзя —
#: непроверенное событие в ленте нечем оспорить (ADR-0015).
WEBHOOK_SECRET_FIELD = CredentialField(
    "webhook_secret",
    "Секрет подписи вебхуков",
    required=False,
)

CREDENTIAL_SCHEMAS: dict[str, CredentialSchema] = {
    "major": CredentialSchema(
        fields=(
            CredentialField("login", "Логин веб-сервиса", secret=False),
            CredentialField("password", "Пароль веб-сервиса"),
            WEBHOOK_SECRET_FIELD,
        ),
        where_to_get="Выдаёт менеджер Major Express вместе с доступом к веб-сервису.",
    ),
    "cdek": CredentialSchema(
        fields=(
            CredentialField("client_id", "Идентификатор клиента", secret=False),
            CredentialField("client_secret", "Секрет клиента"),
            WEBHOOK_SECRET_FIELD,
        ),
        where_to_get="Личный кабинет СДЭК, раздел интеграции: пара для OAuth.",
    ),
    # Состав сверен по официальной OpenAPI перевозчика (ADR-0020): ключ
    # приложения идёт в теле каждого запроса, а вход в кабинет даёт сессию
    # на 30 дней и вместе с ней персональные скидки контрагента.
    "dellin": CredentialSchema(
        fields=(
            CredentialField("appkey", "Ключ приложения"),
            # Токен предпочтительнее пары логин-пароль: он отзывается
            # в кабинете и не открывает доступ ко всему остальному.
            CredentialField("pat", "Токен личного кабинета", required=False),
            CredentialField("login", "Логин личного кабинета", secret=False, required=False),
            CredentialField("password", "Пароль личного кабинета", required=False),
            WEBHOOK_SECRET_FIELD,
        ),
        where_to_get=(
            "Ключ приложения выдаётся при регистрации на dev.dellin.ru и приходит "
            "на почту. Токен генерируется в личном кабинете dellin.ru. Без токена "
            "или пары логин-пароль расчёт вернёт публичный тариф, а не цену "
            "по вашему договору."
        ),
    ),
}

#: Перевозчики из пилота, чей адаптер ещё не написан (Почта России добавлена
#: шестым перевозчиком решением ADR-0020). Состав доступов появится вместе
#: с адаптером, в том же коммите: пара «логин-пароль» подходит не всем —
#: у Почты трекинг и оформление авторизуются порознь, — и объявить поле
#: раньше, чем прочитан способ авторизации, значит попросить у клиента не то.
PENDING_CARRIERS: frozenset[str] = frozenset({"pecom", "pochta", "yandex"})


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
    # Необязательные не считаются недостающими: перевозчик без секрета
    # подписи вебхуков подключён и работает, просто на опросе.
    return [name for name in schema.required_names if not credentials.get(name)]
