"""Конфигурация приложения. Только переменные окружения.

Правило раздела 9.2 ТЗ: приложение **не стартует**, если отсутствует обязательная
переменная. Падать при старте лучше, чем работать наполовину.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

Environment = Literal["local", "staging", "production"]


class Settings(BaseSettings):
    """Настройки приложения.

    Значения по умолчанию допустимы только там, где они безопасны в проде.
    Секреты значений по умолчанию не имеют — их отсутствие роняет старт.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: Environment = "local"
    debug: bool = False
    app_name: str = "Aerogram Logistic OS"
    public_base_url: str = "http://localhost:8000"

    # --- База данных -------------------------------------------------------
    #: Роль приложения. Обязательно БЕЗ атрибута BYPASSRLS (раздел 7.2 ТЗ).
    database_url: PostgresDsn
    #: Отдельная роль для миграций, у неё права на DDL.
    database_migration_url: PostgresDsn | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- Redis -------------------------------------------------------------
    redis_url: RedisDsn
    celery_broker_url: RedisDsn | None = None
    celery_result_backend: RedisDsn | None = None

    # --- Безопасность ------------------------------------------------------
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14

    #: Набор ключей шифрования учётных данных ТК: "key_id:base64,key_id2:base64".
    credential_keys: str
    credential_active_key_id: str = "k1"

    # --- Расчёт (FR-1.3, FR-1.6) -------------------------------------------
    carrier_timeout_seconds: float = 3.0
    rating_deadline_seconds: float = 5.0
    quote_cache_ttl_seconds: int = 900
    quote_price_change_threshold_percent: float = 5.0
    idempotency_ttl_seconds: int = 86_400

    # --- Справочники перевозчиков (FR-8.3) ---------------------------------
    #: Таймаут выгрузки справочника. Отдельно от таймаута расчёта и намного
    #: длиннее: расчёт ждёт один тариф и обязан уложиться в секунды, а выгрузка
    #: тянет тысячи ПВЗ постранично. С трёхсекундным таймаутом синхронизация
    #: не завершилась бы никогда, и справочник остался бы пустым.
    carrier_refs_timeout_seconds: float = 300.0

    # --- Хранение ----------------------------------------------------------
    raw_call_retention_days: int = 30

    # --- Внешние сервисы ---------------------------------------------------
    s3_endpoint_url: str | None = None
    s3_bucket: str = "aerogram"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "ru-1"

    #: Адрес веб-сервиса Major Express, если он отличается от указанного в WSDL.
    #: Логин и пароль сюда НЕ попадают: учётные данные перевозчика у каждого
    #: тенанта свои и лежат зашифрованными в ``carrier_accounts`` (ADR-0005).
    major_express_endpoint_url: str | None = None

    dadata_token: str | None = None
    dadata_secret: str | None = None

    telegram_bot_token: str | None = None
    telegram_alert_chat_id: str | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "noreply@app.aerogram.ru"

    sentry_dsn: str | None = None

    # --- Логи --------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("credential_keys")
    @classmethod
    def _validate_credential_keys(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("credential_keys не может быть пустым")
        for pair in value.split(","):
            if ":" not in pair:
                raise ValueError(
                    "credential_keys ожидается в формате 'key_id:base64[,key_id2:base64]'"
                )
        return value

    @property
    def credential_key_map(self) -> dict[str, str]:
        """Разобранный набор ключей шифрования."""
        return {
            pair.split(":", 1)[0].strip(): pair.split(":", 1)[1].strip()
            for pair in self.credential_keys.split(",")
            if pair.strip()
        }

    @property
    def broker_url(self) -> str:
        return str(self.celery_broker_url or self.redis_url)

    @property
    def result_backend(self) -> str:
        return str(self.celery_result_backend or self.redis_url)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Настройки процесса. Кэшируются: конфигурация не меняется в рантайме."""
    # Значения обязательных полей приходят из окружения, а не из аргументов.
    return Settings()
