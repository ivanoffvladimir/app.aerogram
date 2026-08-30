"""Транспорт Major Express. Пока — заглушка, которая честно говорит, чего нет.

Три вещи должны сойтись, чтобы адаптер заработал (ADR-0013):

1. **WSDL** — файл в ``carriers/major/wsdl/``, в репозитории, а не по сети
   в рантайме: контракт внешней системы обязан меняться видимо, отдельным
   коммитом с читаемым диффом.
2. **SOAP-клиент** ``zeep`` поверх нашего ``httpx``-транспорта — общие
   таймауты, ретраи, circuit breaker и снятие сырья вызовов.
3. **Учётные данные** — логин и пароль Basic Auth, по одному комплекту
   на тенанта, в ``carrier_accounts.credentials_encrypted``. В переменных
   окружения их нет и быть не может: у каждого клиента свой договор.
   Состав полей объявлен в ``carriers/credentials.py``.

Пока нет первого, остальные два бессмысленны, поэтому клиент отказывает
сразу и с указанием причины, а не падает где-то в глубине SOAP.
"""

from __future__ import annotations

from pathlib import Path

from aerogram.carriers.credentials import missing_fields
from aerogram.shared.errors import CarrierNotConfigured

__all__ = ["CARRIER_CODE", "WSDL_DIR", "MajorExpressClient", "wsdl_path"]

CARRIER_CODE = "major"

#: Каталог, куда кладётся WSDL. Имя файла не фиксируем: поставщик волен
#: назвать его по-своему, а версия должна быть видна в имени.
WSDL_DIR = Path(__file__).parent / "wsdl"


def wsdl_path() -> Path | None:
    """Найти WSDL в пакете. ``None`` — файла нет, интеграция не настроена."""
    candidates = sorted(WSDL_DIR.glob("*.wsdl"))
    return candidates[0] if candidates else None


class MajorExpressClient:
    """Клиент веб-сервиса. Настоящие вызовы появятся вместе с WSDL."""

    def __init__(self, credentials: dict[str, str], *, endpoint_url: str | None = None) -> None:
        self._credentials = credentials
        self._endpoint_url = endpoint_url

    def ensure_ready(self) -> Path:
        """Проверить, что интеграцию есть чем выполнять.

        Проверяется и WSDL, и полнота учётных данных: без второго вызов уйдёт
        к перевозчику без авторизации и вернётся невнятной SOAP-ошибкой.
        Пароль в сообщение и в лог не попадает — только имена недостающих полей.
        """
        missing = missing_fields(CARRIER_CODE, self._credentials)
        if missing:
            raise CarrierNotConfigured(
                "В учётной записи Major Express не заполнены поля: " + ", ".join(missing),
                carrier_code=CARRIER_CODE,
            )

        path = wsdl_path()
        if path is None:
            raise CarrierNotConfigured(
                "Описание веб-сервиса Major Express (WSDL) не добавлено в проект",
                carrier_code=CARRIER_CODE,
            )
        return path
