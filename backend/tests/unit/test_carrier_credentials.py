"""Состав доступов перевозчика: что обязательно, а что включает возможность.

Секрет подписи вебхуков — второй случай в проекте, когда поле доступов
необязательно. Разница существенная: без обязательного поля перевозчик
не работает вовсе, а без секрета подписи он работает на опросе, просто
без вебхуков. Спутать их значит либо не дать подключить перевозчика,
либо молча принимать непроверенные события.
"""

from __future__ import annotations

import pytest

from aerogram.carriers.credentials import (
    CREDENTIAL_SCHEMAS,
    WEBHOOK_SECRET_FIELD,
    missing_fields,
    schema_for,
)
from aerogram.tracking.inbound import CREDENTIAL_FIELD


class TestTheWebhookSecretIsSettable:
    def test_the_field_name_matches_the_one_the_receiver_reads(self) -> None:
        """Иначе секрет вводится, а проверка подписи его не находит.

        Разошлись бы эти два имени — вебхуки молча перестали бы приниматься,
        и выглядело бы это как «перевозчик их не шлёт».
        """
        assert WEBHOOK_SECRET_FIELD.name == CREDENTIAL_FIELD

    @pytest.mark.parametrize("code", sorted(CREDENTIAL_SCHEMAS))
    def test_every_known_carrier_asks_for_it(self, code: str) -> None:
        """Поле, которого нет в описании, кабинет не покажет и скрипт
        не спросит — задать его будет негде."""
        schema = schema_for(code)
        assert schema is not None
        assert CREDENTIAL_FIELD in schema.names

    def test_it_is_a_secret(self) -> None:
        assert WEBHOOK_SECRET_FIELD.secret is True

    def test_it_is_not_required(self) -> None:
        """Требовать его значило бы не дать подключить перевозчика тому,
        кому вебхуки не нужны."""
        assert WEBHOOK_SECRET_FIELD.required is False


class TestMissingFields:
    def test_an_absent_optional_field_is_not_missing(self) -> None:
        assert missing_fields("cdek", {"client_id": "i", "client_secret": "s"}) == []

    def test_an_absent_required_field_is_missing(self) -> None:
        assert missing_fields("cdek", {"client_id": "i"}) == ["client_secret"]

    def test_an_unknown_carrier_reports_nothing(self) -> None:
        """Пустой список у неизвестного — «нечем проверить», а не «всё в порядке»:
        требовать поля, состава которых мы не знаем, вредно."""
        assert missing_fields("dellin", {}) == []
