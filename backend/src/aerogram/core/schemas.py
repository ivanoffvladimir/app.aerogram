"""DTO ядра. Контракт между слоями и с фронтом (генерируется в клиент из OpenAPI)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from aerogram.shared.enums import TenantRole, TenantStatus, UserRole

__all__ = [
    "ErrorBody",
    "ErrorResponse",
    "LoginRequest",
    "RefreshRequest",
    "TenantOut",
    "TokenPair",
    "UserCreate",
    "UserOut",
]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    #: Код TOTP. Обязателен для роли owner (12.5 ТЗ).
    mfa_code: str | None = Field(default=None, min_length=6, max_length=6)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105  # схема авторизации, не пароль
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    mfa_enabled: bool
    last_login_at: datetime | None = None


class UserCreate(BaseModel):
    """Новый пользователь тенанта.

    ``role`` — ``TenantRole``, а не ``UserRole``: платформенных ролей в этом
    перечислении нет физически, поэтому владелец тенанта не может выдать себе
    доступ к общим справочникам, которые читаются на расчёте всех тенантов.
    """

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    role: TenantRole
    password: str = Field(min_length=12, max_length=128)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    inn: str | None = None
    status: TenantStatus
    plan: str
    timezone: str


class ErrorBody(BaseModel):
    """Единый формат ошибки (FR-10.5)."""

    code: str
    message: str
    field: str | None = None
    carrier_code: str | None = None
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class AddressCreate(BaseModel):
    """Новый адрес контрагента.

    ``city_fias_id`` не проверяется на существование в справочнике синхронно:
    в интерфейсе город всегда приходит из подсказки, которая сама записывает
    его в ``cities``, а блокировать сохранение адреса из-за недоступности
    внешнего справочника нельзя.
    """

    label: str | None = Field(default=None, max_length=255)
    country_code: str = Field(default="RU", min_length=2, max_length=2)
    region: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=255)
    city_fias_id: str | None = Field(default=None, min_length=36, max_length=36)
    city_parent_fias_id: str | None = Field(default=None, min_length=36, max_length=36)
    postal_code: str | None = Field(default=None, max_length=10)
    street: str | None = Field(default=None, max_length=255)
    house: str | None = Field(default=None, max_length=50)
    flat: str | None = Field(default=None, max_length=50)
    lat: float | None = None
    lon: float | None = None
    is_default_sender: bool = False
    comment: str | None = None


class AddressUpdate(BaseModel):
    """Изменение адреса. Не переданные поля не трогаются."""

    label: str | None = Field(default=None, max_length=255)
    region: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, min_length=1, max_length=255)
    city_fias_id: str | None = Field(default=None, min_length=36, max_length=36)
    postal_code: str | None = Field(default=None, max_length=10)
    street: str | None = Field(default=None, max_length=255)
    house: str | None = Field(default=None, max_length=50)
    flat: str | None = Field(default=None, max_length=50)
    is_default_sender: bool | None = None
    comment: str | None = None


class AddressOut(BaseModel):
    """Адрес контрагента."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    counterparty_id: UUID
    label: str | None = None
    country_code: str
    region: str | None = None
    city: str
    city_fias_id: str | None = None
    city_parent_fias_id: str | None = None
    postal_code: str | None = None
    street: str | None = None
    house: str | None = None
    flat: str | None = None
    lat: float | None = None
    lon: float | None = None
    is_default_sender: bool
    #: Для чего адрес годится: door / locality / unusable.
    fitness: str
    comment: str | None = None


class CounterpartyCreate(BaseModel):
    """Новый контрагент адресной книги (FR-8.4)."""

    type: Literal["legal", "individual", "entrepreneur"]
    name: str = Field(min_length=1, max_length=500)
    inn: str | None = Field(default=None, min_length=10, max_length=12, pattern=r"^\d+$")
    kpp: str | None = Field(default=None, min_length=9, max_length=9, pattern=r"^\d+$")
    contact_person: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    addresses: list[AddressCreate] = Field(default_factory=list)


class CounterpartyUpdate(BaseModel):
    """Изменение контрагента."""

    name: str | None = Field(default=None, min_length=1, max_length=500)
    contact_person: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None


class CounterpartyOut(BaseModel):
    """Контрагент с адресами."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: str
    name: str
    inn: str | None = None
    kpp: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    addresses: list[AddressOut] = Field(default_factory=list)


class Page[T](BaseModel):
    """Постраничная выдача. Формат единый для всего API."""

    items: list[T]
    total: int
    limit: int
    offset: int
