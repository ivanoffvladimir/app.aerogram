"""Логика ядра: аутентификация, пользователи, API-ключи, аудит."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aerogram.config import Settings
from aerogram.core.models import (
    Address,
    ApiKey,
    AuditLog,
    CarrierAccount,
    Counterparty,
    Tenant,
    User,
)
from aerogram.core.repository import (
    AddressRepository,
    ApiKeyRepository,
    AuditRepository,
    CounterpartyRepository,
    TenantRepository,
    UserRepository,
)
from aerogram.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    needs_rehash,
    verify_password,
)
from aerogram.db import set_tenant
from aerogram.shared import totp
from aerogram.shared.addresses import assess_fitness
from aerogram.shared.clock import utcnow
from aerogram.shared.crypto import CredentialCipher
from aerogram.shared.enums import PLATFORM_ROLES, TenantStatus, UserRole
from aerogram.shared.errors import (
    AuthenticationError,
    Conflict,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from aerogram.shared.logging import get_logger

__all__ = [
    "AUTH_SCOPE_SETTING",
    "ApiKeyService",
    "AuthResult",
    "AuthService",
    "MfaService",
    "UserService",
]

log = get_logger(__name__)

#: Настройка сессии, открывающая узкое окно на поиск пользователя при входе.
#:
#: Единственное место, где она устанавливается, — ``AuthService._lookup_user`` ниже.
#: Причина существования: вход выполняется до того, как известен тенант, поэтому
#: обычная политика RLS по ``app.tenant_id`` его не находит. Окно транзакционное
#: (``set_config(..., true)``), доступно только на SELECT и закрывается автоматически.
#: Тест ``tests/unit/test_auth_scope_guard.py`` следит, чтобы других мест не появилось.
AUTH_SCOPE_SETTING = "app.auth_scope"
_AUTH_SCOPE_VALUE = "login"
#: Второе окно — поиск API-ключа. Тенант определяется ПО ключу, поэтому до
#: поиска его знать неоткуда, и RLS без установленного тенанта не отдаёт строк.
#: Значение отдельное от логина: окно на users не должно открывать api_keys
#: и наоборот.
_API_KEY_SCOPE_VALUE = "api_key"


@dataclass(frozen=True, slots=True)
class AuthResult:
    """Результат успешного входа."""

    user_id: UUID
    tenant_id: UUID
    role: UserRole
    access_token: str
    refresh_token: str
    expires_in: int


class AuthService:
    """Вход, обновление токена, разбор токена."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._users = UserRepository(session)
        self._tenants = TenantRepository(session)

    async def login(self, email: str, password: str, mfa_code: str | None) -> AuthResult:
        """Проверить учётные данные и выдать пару токенов.

        Причина сбоя наружу не раскрывается: «неверный e-mail» и «неверный пароль» —
        один и тот же ответ, иначе получается оракул для перебора учётных записей.
        """
        user = await self._lookup_user(email)
        if user is None or not verify_password(user.password_hash, password):
            log.info("auth.login_failed", email=email)
            raise AuthenticationError("Неверный e-mail или пароль")

        if not user.is_active:
            raise AuthenticationError("Учётная запись отключена")

        tenant = await self._tenants.get_by_id(user.tenant_id)
        if tenant is None or tenant.status == TenantStatus.SUSPENDED:
            raise AuthenticationError("Доступ компании приостановлен")

        # Двухфакторная аутентификация обязательна для роли owner (12.5 ТЗ).
        if user.role == UserRole.OWNER and not user.mfa_enabled:
            log.warning("auth.owner_without_mfa", user_id=str(user.id))
        # Код сверяется ДО установки тенанта — на чтение секрета хватает окна
        # входа. Списание шага требует записи и потому идёт ниже, под RLS.
        step = verify_totp(user, mfa_code, self._settings) if user.mfa_enabled else None

        await set_tenant(self._session, user.tenant_id)
        if step is not None and not await self._users.consume_mfa_step(user.id, step):
            # Шаг уже предъявляли: код подсмотрен и повторён в пределах своих
            # тридцати секунд. Ради этого случая второй фактор и существует.
            log.warning("auth.mfa_replay", user_id=str(user.id))
            raise AuthenticationError("Неверный код второго фактора")
        await self._users.touch_login(user.id)

        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        log.info("auth.login_ok", user_id=str(user.id), tenant_id=str(user.tenant_id))
        return self._issue(user.id, user.tenant_id, UserRole(user.role))

    async def refresh(self, refresh_token: str) -> AuthResult:
        """Обменять refresh-токен на новую пару."""
        payload = decode_token(self._settings, refresh_token)
        if payload.token_type != "refresh":  # noqa: S105  # тип токена, не пароль
            raise AuthenticationError("Ожидался refresh-токен")

        await set_tenant(self._session, payload.tenant_id)
        user = await self._users.get_by_id(payload.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Учётная запись недоступна")

        return self._issue(user.id, user.tenant_id, UserRole(user.role))

    def _issue(self, user_id: UUID, tenant_id: UUID, role: UserRole) -> AuthResult:
        return AuthResult(
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            access_token=create_access_token(self._settings, user_id, tenant_id, role.value),
            refresh_token=create_refresh_token(self._settings, user_id, tenant_id, role.value),
            expires_in=self._settings.access_token_ttl_minutes * 60,
        )

    async def _lookup_user(self, email: str) -> User | None:
        """Найти пользователя по e-mail до того, как известен тенант.

        См. комментарий к ``AUTH_SCOPE_SETTING``: это единственное место в проекте,
        где открывается окно поиска поверх RLS, и оно закрывается вместе с транзакцией.
        """
        await self._session.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": AUTH_SCOPE_SETTING, "value": _AUTH_SCOPE_VALUE},
        )
        try:
            return await self._users.get_by_email(email.lower())
        finally:
            await self._session.execute(
                text("SELECT set_config(:name, '', true)"), {"name": AUTH_SCOPE_SETTING}
            )


def verify_totp(user: User, code: str | None, settings: Settings) -> int:
    """Сверить код второго фактора и вернуть номер сожжённого шага.

    Возвращается именно шаг, а не «да/нет»: без него нечем отсечь повтор
    подсмотренного кода (``shared.totp``, ADR-0018). Списывает шаг вызывающий —
    здесь нет транзакции.

    Секрет хранится зашифрованным с привязкой к идентификатору пользователя,
    поэтому перенос шифротекста в чужую строку не расшифруется.
    """
    if code is None:
        raise AuthenticationError("Требуется код второго фактора")
    if user.mfa_secret is None:
        # Второй фактор включён, а секрета нет: чинить это входом нельзя.
        log.error("auth.mfa_secret_missing", user_id=str(user.id))
        raise AuthenticationError("Второй фактор настроен неверно: обратитесь к администратору")

    secret = decrypt_mfa_secret(user, settings)
    step = totp.verify(secret, code, at=utcnow())
    if step is None:
        log.info("auth.mfa_failed", user_id=str(user.id))
        raise AuthenticationError("Неверный код второго фактора")
    return step


def encrypt_mfa_secret(user_id: UUID, secret: str, settings: Settings) -> str:
    """Зашифровать секрет TOTP. AAD — идентификатор пользователя (ADR-0018)."""
    cipher = CredentialCipher(settings.credential_key_map, settings.credential_active_key_id)
    return cipher.encrypt(secret, aad=_mfa_aad(user_id))


def decrypt_mfa_secret(user: User, settings: Settings) -> str:
    """Расшифровать секрет TOTP владельца записи."""
    if user.mfa_secret is None:
        raise AuthenticationError("Второй фактор не подключён")
    cipher = CredentialCipher(settings.credential_key_map, settings.credential_active_key_id)
    return cipher.decrypt(user.mfa_secret, aad=_mfa_aad(user.id))


def _mfa_aad(user_id: UUID) -> bytes:
    """Метка назначения шифротекста.

    Префикс отделяет секреты TOTP от учётных данных перевозчиков: у них общий
    набор ключей, и без метки шифротекст одного вида в теории подставляется
    в поле другого.
    """
    return f"mfa:{user_id}".encode()


class MfaService:
    """Подключение и отключение второго фактора (12.5 ТЗ).

    Секрет отдаётся ровно один раз — в ответе на подключение. Повторно его
    прочитать нельзя ни через какой эндпоинт: иначе украденная сессия
    превращается в постоянный обход второго фактора.
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._users = UserRepository(session)

    async def setup(self, user_id: UUID) -> tuple[str, str]:
        """Выдать новый секрет. Возвращает пару «секрет, ссылка otpauth»."""
        user = await self._require_user(user_id)
        if user.mfa_enabled:
            # Перевыпуск при включённом факторе — это его тихая замена.
            # Сначала отключение с подтверждением кодом, потом новый секрет.
            raise Conflict("Второй фактор уже подключён: сначала отключите его")

        secret = totp.generate_secret()
        user.mfa_secret = encrypt_mfa_secret(user.id, secret, self._settings)
        user.mfa_last_step = None
        log.info("auth.mfa_setup", user_id=str(user.id))
        return secret, totp.provisioning_uri(
            secret, account_name=user.email, issuer=self._settings.app_name
        )

    async def enable(self, user_id: UUID, code: str) -> User:
        """Включить второй фактор, подтвердив владение секретом."""
        user = await self._require_user(user_id)
        if user.mfa_enabled:
            raise Conflict("Второй фактор уже подключён")
        if user.mfa_secret is None:
            raise ValidationFailed("Секрет не выпущен: начните с подключения", field="code")

        await self._burn(user, code)
        user.mfa_enabled = True
        log.info("auth.mfa_enabled", user_id=str(user.id))
        return user

    async def disable(self, user_id: UUID, code: str) -> User:
        """Отключить второй фактор. Код обязателен: иначе это делает любая сессия."""
        user = await self._require_user(user_id)
        if not user.mfa_enabled:
            raise Conflict("Второй фактор не подключён")

        await self._burn(user, code)
        user.mfa_enabled = False
        user.mfa_secret = None
        user.mfa_last_step = None
        if user.role == UserRole.OWNER:
            # 12.5 ТЗ требует второй фактор для владельца. Вход его отсутствие
            # пока не запрещает — см. docs/status.md, решение за человеком.
            log.warning("auth.owner_mfa_disabled", user_id=str(user.id))
        log.info("auth.mfa_disabled", user_id=str(user.id))
        return user

    async def _burn(self, user: User, code: str) -> None:
        step = verify_totp(user, code, self._settings)
        if not await self._users.consume_mfa_step(user.id, step):
            log.warning("auth.mfa_replay", user_id=str(user.id))
            raise AuthenticationError("Неверный код второго фактора")

    async def _require_user(self, user_id: UUID) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFound("Пользователь не найден")
        return user


class UserService:
    """Пользователи тенанта."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)

    async def get(self, user_id: UUID) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None:
            # 404, а не 403: подтверждать существование чужого объекта нельзя
            # (раздел 14.1 ТЗ, тест изоляции тенантов).
            raise NotFound("Пользователь не найден")
        return user

    async def list_active(self) -> list[User]:
        return await self._users.list_active()

    async def create(
        self, *, tenant_id: UUID, email: str, full_name: str, role: UserRole, password: str
    ) -> User:
        if role in PLATFORM_ROLES:
            # Второй барьер после типа в схеме: подъём привилегий стоит слишком
            # дорого, чтобы держать защиту в одном месте. Платформенная роль
            # выдаётся вне продуктового API, а не владельцем тенанта.
            raise ValidationFailed("Платформенная роль не выдаётся через API тенанта", field="role")
        if await self._users.get_by_email(email) is not None:
            raise Conflict("Пользователь с таким e-mail уже существует в этой компании")

        user = User(
            tenant_id=tenant_id,
            email=email.lower(),
            full_name=full_name,
            role=role,
            password_hash=hash_password(password),
            is_active=True,
        )
        self._users.add(user)
        await self._session.flush()
        return user


class ApiKeyService:
    """Выпуск и отзыв API-ключей (FR-10.2)."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._keys = ApiKeyRepository(session)

    async def issue(self, *, tenant_id: UUID, name: str, scopes: list[str]) -> tuple[ApiKey, str]:
        """Выпустить ключ. Полное значение возвращается один раз и не хранится."""
        full, prefix, key_hash = generate_api_key(self._settings.environment)
        key = ApiKey(
            tenant_id=tenant_id, name=name, prefix=prefix, key_hash=key_hash, scopes=scopes
        )
        self._keys.add(key)
        await self._session.flush()
        return key, full

    async def list(self) -> list[ApiKey]:
        """Действующие ключи тенанта. Отозванные не показываются."""
        return await self._keys.list_for_tenant()

    async def revoke(self, key_id: UUID) -> None:
        keys = await self._keys.list_for_tenant()
        target = next((k for k in keys if k.id == key_id), None)
        if target is None:
            raise NotFound("Ключ не найден")
        target.revoked_at = utcnow()

    async def resolve(self, raw_key: str) -> ApiKey:
        """Определить тенанта по предъявленному ключу.

        Открывает узкое окно поверх RLS — по той же причине и по тому же
        образцу, что и поиск пользователя при входе (ADR-0004): тенанта мы
        как раз и определяем по ключу, а до этого политика не пропускает
        ни одной строки. Окно только на SELECT и закрывается вместе
        с транзакцией.
        """
        key = await self._lookup_key(hash_api_key(raw_key))
        if key is None:
            raise AuthenticationError("Ключ недействителен")
        if key.expires_at is not None and key.expires_at < utcnow():
            raise AuthenticationError("Срок действия ключа истёк")
        return key

    async def mark_used(self, key_id: UUID) -> None:
        """Отметить факт использования ключа.

        Вызывается ТОЛЬКО после ``set_tenant``. Раньше отметка стояла внутри
        ``resolve``, то есть до установки тенанта: окно поиска открыто лишь
        на SELECT, а ``tenant_isolation`` без ``app.tenant_id`` не пропускает
        ничего — ``UPDATE`` молча не находил строк, и ``last_used_at``
        не записывался никогда. При компрометации ключа было бы не видно,
        пользовались им или нет.
        """
        await self._keys.touch_used(key_id)

    async def _lookup_key(self, key_hash: str) -> ApiKey | None:
        await self._session.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": AUTH_SCOPE_SETTING, "value": _API_KEY_SCOPE_VALUE},
        )
        try:
            return await self._keys.get_by_hash(key_hash)
        finally:
            await self._session.execute(
                text("SELECT set_config(:name, '', true)"), {"name": AUTH_SCOPE_SETTING}
            )


def decrypt_credentials(account: CarrierAccount, settings: Settings) -> dict[str, str]:
    """Расшифровать учётные данные перевозчика.

    Живёт в ядре, потому что учётная запись — сущность ядра, и потому что
    расшифровка нужна и расчёту, и созданию отправления: две копии рано или
    поздно разошлись бы в том, как обращаются с ключами.

    Привязка к идентификатору записи: перенос шифротекста в чужую строку
    не расшифруется (см. ``shared.crypto``). Исключение наружу не гасится —
    вызывающий слой сам решает, пропустить ли перевозчика или отказать.
    """
    cipher = CredentialCipher(settings.credential_key_map, settings.credential_active_key_id)
    raw = cipher.decrypt(account.credentials_encrypted, aad=str(account.id).encode())
    parsed = json.loads(raw)
    return {str(k): str(v) for k, v in parsed.items()}


class AuditService:
    """Запись в аудит-лог (12.6 ТЗ)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditRepository(session)

    def record(
        self,
        *,
        tenant_id: UUID,
        actor_user_id: UUID | None,
        action: str,
        entity_type: str,
        entity_id: UUID | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        payload_diff: dict[str, object] | None = None,
        impersonated_by_user_id: UUID | None = None,
    ) -> None:
        self._audit.record(
            AuditLog(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                impersonated_by_user_id=impersonated_by_user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                ip=ip,
                user_agent=user_agent,
                payload_diff=payload_diff,
            )
        )


def require_role(actual: UserRole, allowed: frozenset[UserRole]) -> None:
    """Проверка прав (раздел 2 ТЗ). Недостаток прав — 403, отсутствие объекта — 404."""
    if actual not in allowed:
        raise PermissionDenied()


def ensure_tenant_active(tenant: Tenant) -> None:
    """Приостановленный тенант не может выполнять изменяющие операции."""
    if tenant.status == TenantStatus.SUSPENDED:
        raise PermissionDenied("Доступ компании приостановлен")


class AddressBookService:
    """Адресная книга тенанта: контрагенты и их адреса (FR-8.4)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._counterparties = CounterpartyRepository(session)
        self._addresses = AddressRepository(session)

    async def get(self, counterparty_id: UUID) -> Counterparty:
        counterparty = await self._counterparties.get_by_id(counterparty_id)
        if counterparty is None:
            # 404, а не 403: чужой контрагент не должен подтверждать своё
            # существование (критерий приёмки 14.2, п. 11).
            raise NotFound("Контрагент не найден")
        return counterparty

    async def search(
        self, query: str | None, *, limit: int, offset: int
    ) -> tuple[list[Counterparty], int]:
        return await self._counterparties.search(query, limit=limit, offset=offset)

    async def create(
        self,
        *,
        tenant_id: UUID,
        type_: str,
        name: str,
        inn: str | None,
        kpp: str | None,
        contact_person: str | None,
        phone: str | None,
        email: str | None,
        addresses: list[dict[str, object]],
    ) -> Counterparty:
        """Завести контрагента вместе с адресами."""
        if inn is not None and await self._counterparties.get_by_inn(inn, kpp) is not None:
            raise Conflict("Контрагент с таким ИНН уже есть в адресной книге")

        counterparty = Counterparty(
            tenant_id=tenant_id,
            type=type_,
            name=name,
            inn=inn,
            kpp=kpp,
            contact_person=contact_person,
            phone=phone,
            email=email,
            # Пустой список инициализирует коллекцию как загруженную. Без этого
            # первое же обращение к .addresses после flush пытается дочитать её
            # ленивым запросом вне асинхронного контекста.
            addresses=[],
        )
        self._counterparties.add(counterparty)
        await self._session.flush()

        for payload in addresses:
            await self.add_address(tenant_id=tenant_id, counterparty=counterparty, payload=payload)
        return counterparty

    async def add_address(
        self, *, tenant_id: UUID, counterparty: Counterparty, payload: dict[str, object]
    ) -> Address:
        """Добавить адрес контрагенту."""
        is_default = bool(payload.get("is_default_sender"))
        if is_default:
            # Отправитель по умолчанию ровно один: старый снимается до вставки
            # нового, иначе частичный уникальный индекс отвергнет запись.
            await self._addresses.clear_default_sender()

        # Пригодность вычисляется по тому, что известно об адресе, а не берётся
        # из значения по умолчанию. Иначе адрес с городом, улицей и домом,
        # введённый в адресной книге, навсегда оставался бы непригодным
        # и блокировал создание отправления до двери.
        fitness, _ = assess_fitness(
            city_known=bool(payload.get("city_fias_id")),
            house_known=bool(payload.get("house")),
            foreign=str(payload.get("country_code") or "RU") != "RU",
        )
        address = Address(
            tenant_id=tenant_id,
            counterparty_id=counterparty.id,
            **{k: v for k, v in payload.items() if k != "is_default_sender"},
            is_default_sender=is_default,
            fitness=fitness.value,
        )
        # Адрес добавляется В КОЛЛЕКЦИЮ контрагента, а не отдельной вставкой:
        # иначе связь остаётся незагруженной, и сериализация ответа пытается
        # дочитать её ленивым запросом уже вне асинхронного контекста.
        counterparty.addresses.append(address)
        await self._session.flush()
        return address

    async def list_addresses(self, counterparty_id: UUID) -> list[Address]:
        await self.get(counterparty_id)
        return await self._addresses.list_for_counterparty(counterparty_id)

    async def soft_delete(self, counterparty_id: UUID) -> None:
        """Мягкое удаление.

        Физическое невозможно: ``shipments`` ссылается на адреса с
        ``ondelete="RESTRICT"``, а отправления не удаляются никогда
        (раздел 7.1 ТЗ).
        """
        counterparty = await self.get(counterparty_id)
        counterparty.deleted_at = utcnow()
        for address in await self._addresses.list_for_counterparty(counterparty_id):
            address.deleted_at = utcnow()
            address.is_default_sender = False
