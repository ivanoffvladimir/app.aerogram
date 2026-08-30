"""Сборка приложения FastAPI.

Единый origin: SPA, API и публичный трекинг живут на одном домене (раздел 9.1 ТЗ),
поэтому CORS в проде не нужен и намеренно не включён.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aerogram.carriers import registry
from aerogram.config import get_settings
from aerogram.core.router import auth_router, counterparties_router, users_router
from aerogram.db import get_engine
from aerogram.directories.router import admin_directories_router, directories_router
from aerogram.intelligence.router import analytics_router
from aerogram.rating.router import rating_router
from aerogram.routing.router import routing_router
from aerogram.shared.errors import AerogramError, Conflict
from aerogram.shared.logging import configure_logging, get_logger, request_id_var
from aerogram.shipments.router import shipments_router
from aerogram.tracking.router import tracking_router, webhooks_router

__all__ = ["app", "create_app"]

log = get_logger(__name__)

#: Префикс контракта (``docs/tz/v3/openapi.yaml``): пути начинаются с /v1.
API_PREFIX = "/v1"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Старт и остановка приложения."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    log.info("app.start", environment=settings.environment, version=application.version)

    yield

    await get_engine().dispose()
    log.info("app.stop")


def _register_carriers() -> None:
    """Заполнить реестр адаптеров.

    Это композиционный корень: единственное место, которому разрешено знать
    о конкретных перевозчиках. Домен обращается к ним только через
    ``carriers.registry`` — контракт ``no-direct-carrier`` в ``.importlinter``.

    Порядок подключения задан разделом 8.3 ТЗ: СДЭК → Major Express → ПЭК →
    Деловые Линии.
    """
    from aerogram.carriers.cdek import CdekAdapter

    if CdekAdapter.code not in registry.available_codes():
        registry.register(CdekAdapter())


def create_app() -> FastAPI:
    """Собрать приложение."""
    settings = get_settings()

    # Реестр адаптеров заполняется при сборке приложения, а не в lifespan:
    # это чистое состояние процесса без ввода-вывода, а зависящий от lifespan
    # реестр оказывался бы пустым везде, где приложение собирают без запуска —
    # в тестах и при выгрузке схемы OpenAPI.
    _register_carriers()

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "Единый слой между информационными системами грузоотправителя "
            "и транспортными компаниями."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        openapi_tags=[
            {"name": "Аутентификация", "description": "Вход, обновление токена, профиль"},
            {"name": "Пользователи", "description": "Пользователи компании и их роли"},
            {"name": "Адресная книга", "description": "Контрагенты и адреса тенанта"},
            {"name": "Справочники", "description": "Города ФИАС, адреса, терминалы"},
            {"name": "Расчёт", "description": "Стоимость и срок по подключённым перевозчикам"},
            {
                "name": "Администрирование платформы",
                "description": "Очередь ручного сопоставления городов",
            },
            {"name": "Служебное", "description": "Проверки состояния"},
        ],
    )

    @application.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        """Сквозной ``request_id`` через весь стек (раздел 11 ТЗ, наблюдаемость)."""
        request_id = request.headers.get("x-request-id") or secrets.token_urlsafe(12)
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = request_id
        return response

    @application.exception_handler(AerogramError)
    async def handle_domain_error(request: Request, exc: AerogramError) -> JSONResponse:
        """Доменная ошибка → единый формат ответа (FR-10.5)."""
        request_id = request_id_var.get()
        if exc.http_status >= 500:
            log.error("api.error", code=exc.code, message=exc.message_ru, path=request.url.path)
        else:
            log.info("api.error", code=exc.code, path=request.url.path)
        return JSONResponse(status_code=exc.http_status, content=exc.as_payload(request_id))

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Ошибка валидации запроса → тот же формат, с указанием поля."""
        first = exc.errors()[0] if exc.errors() else {}
        location = first.get("loc", ())
        field = ".".join(str(part) for part in location[1:]) or None
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_failed",
                    "message": "Данные не прошли проверку",
                    "field": field,
                    "carrier_code": None,
                    "request_id": request_id_var.get(),
                }
            },
        )

    @application.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        """Нарушение уникального индекса → 409, а не 500.

        Гонка двух операторов на уникальном индексе — штатное событие, а не
        авария. Без этого обработчика ошибка уходит в общий 500, а вместе с ней
        в stdout уезжает текст запроса с параметрами, то есть персональные
        данные (12.7 ТЗ).
        """
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        log.info("api.integrity_error", constraint=constraint, path=request.url.path)
        conflict = Conflict()
        return JSONResponse(
            status_code=conflict.http_status, content=conflict.as_payload(request_id_var.get())
        )

    @application.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Последний рубеж: любая незапланированная ошибка → единый формат.

        В лог уходит только тип исключения и путь. Текст исключения не пишется
        намеренно: у ошибок SQLAlchemy в него входит запрос вместе со
        значениями параметров, а это адреса и телефоны получателей.
        """
        log.error("api.unhandled", error_type=type(exc).__name__, path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Внутренняя ошибка сервиса",
                    "field": None,
                    "carrier_code": None,
                    "request_id": request_id_var.get(),
                }
            },
        )

    @application.get("/health", tags=["Служебное"], summary="Проверка состояния")
    async def health() -> dict[str, str]:
        """Живость процесса. Не ходит в БД: сюда стучится балансировщик."""
        return {"status": "ok"}

    @application.get("/health/ready", tags=["Служебное"], summary="Готовность к работе")
    async def ready() -> dict[str, str]:
        """Готовность: проверяется доступность БД."""
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready"}

    application.include_router(auth_router, prefix=API_PREFIX)
    application.include_router(users_router, prefix=API_PREFIX)
    application.include_router(counterparties_router, prefix=API_PREFIX)
    application.include_router(directories_router, prefix=API_PREFIX)
    application.include_router(admin_directories_router, prefix=API_PREFIX)
    application.include_router(rating_router, prefix=API_PREFIX)
    application.include_router(routing_router, prefix=API_PREFIX)
    application.include_router(shipments_router, prefix=API_PREFIX)
    application.include_router(tracking_router, prefix=API_PREFIX)
    application.include_router(analytics_router, prefix=API_PREFIX)
    application.include_router(webhooks_router, prefix=API_PREFIX)

    return application


app = create_app()
