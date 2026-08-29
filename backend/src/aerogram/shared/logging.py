"""Структурные логи (JSON) с маскированием персональных данных.

12.7 ТЗ: ПДн получателей не попадают в логи и трассировки. Полные значения — только
в БД. Маскирование выполняется процессором structlog, а не на местах вызова: место
вызова забудут, процессор — нет.
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

import structlog

__all__ = ["configure_logging", "get_logger", "mask_pd", "mask_secret", "request_id_var"]

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
tenant_id_var: ContextVar[str] = ContextVar("tenant_id", default="-")

#: Ключи, значения которых считаются персональными данными или секретами.
_PD_KEYS = frozenset(
    {
        "phone",
        "phones",
        "email",
        "full_name",
        "name",
        "contact_person",
        "recipient_name",
        "sender_name",
        "address",
        "street",
        "house",
        "flat",
        "passport",
        "inn",
        "lat",
        "lon",
    }
)
_SECRET_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
        "secret",
        "api_key",
        "key_hash",
        "authorization",
        "credentials",
        "credentials_encrypted",
        "mfa_secret",
    }
)

_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\-\s()]{8,}\d)(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def mask_secret(value: object) -> str:
    """Полностью скрыть значение, оставив длину для отладки."""
    text = str(value)
    return f"<скрыто:{len(text)}>"


def mask_pd(value: object) -> str:
    """Замаскировать персональные данные, оставив опознаваемый хвост.

    Оставленных символов достаточно, чтобы сопоставить строку лога с записью в БД,
    и недостаточно, чтобы идентифицировать человека по логу.
    """
    text = str(value)
    if not text:
        return text
    if "@" in text:
        local, _, domain = text.partition("@")
        head = local[:1] if local else ""
        return f"{head}***@{domain}"
    if len(text) <= 4:
        return "***"
    return f"***{text[-4:]}"


def _mask_text(text: str) -> str:
    text = _EMAIL_RE.sub(lambda m: mask_pd(m.group(0)), text)
    return _PHONE_RE.sub(lambda m: mask_pd(m.group(0)), text)


def _mask_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if lowered in _SECRET_KEYS:
        return mask_secret(value)
    if lowered in _PD_KEYS:
        return mask_pd(value)
    if isinstance(value, dict):
        return {k: _mask_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_value(key, v) for v in value]
    if isinstance(value, str):
        return _mask_text(value)
    return value


def _mask_processor(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    return {k: _mask_value(k, v) for k, v in event_dict.items()}


def _context_processor(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    event_dict.setdefault("request_id", request_id_var.get())
    tenant = tenant_id_var.get()
    if tenant != "-":
        event_dict.setdefault("tenant_id", tenant)
    return event_dict


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Настроить structlog и стандартный logging единообразно."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _context_processor,
            _mask_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Логгер модуля."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
