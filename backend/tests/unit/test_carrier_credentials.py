"""Состав доступов перевозчика: что обязательно, а что включает возможность.

Секрет подписи вебхуков — второй случай в проекте, когда поле доступов
необязательно. Разница существенная: без обязательного поля перевозчик
не работает вовсе, а без секрета подписи он работает на опросе, просто
без вебхуков. Спутать их значит либо не дать подключить перевозчика,
либо молча принимать непроверенные события.
"""

from __future__ import annotations

import pytest

from aerogram.carriers.cdek import CdekAdapter
from aerogram.carriers.credentials import (
    CREDENTIAL_SCHEMAS,
    WEBHOOK_SECRET_FIELD,
    missing_fields,
    schema_for,
)
from aerogram.carriers.dellin import DellinAdapter
from aerogram.carriers.major.adapter import MajorExpressAdapter
from aerogram.carriers.pecom import PecomAdapter
from aerogram.carriers.pochta import PochtaAdapter
from aerogram.tracking.inbound import CREDENTIAL_FIELD

#: Кто из объявленных перевозчиков шлёт вебхуки. Берётся из самих адаптеров,
#: а не выписывается руками: иначе таблица разойдётся с кодом ровно тогда,
#: когда у перевозчика появятся или исчезнут вебхуки.
_SUPPORTS_WEBHOOKS = {
    adapter.code: adapter.capabilities.supports_webhooks
    for adapter in (
        CdekAdapter,
        DellinAdapter,
        PecomAdapter,
        PochtaAdapter,
        MajorExpressAdapter,
    )
}


class TestTheWebhookSecretIsSettable:
    def test_the_field_name_matches_the_one_the_receiver_reads(self) -> None:
        """Иначе секрет вводится, а проверка подписи его не находит.

        Разошлись бы эти два имени — вебхуки молча перестали бы приниматься,
        и выглядело бы это как «перевозчик их не шлёт».
        """
        assert WEBHOOK_SECRET_FIELD.name == CREDENTIAL_FIELD

    @pytest.mark.parametrize("code", sorted(CREDENTIAL_SCHEMAS))
    def test_a_carrier_with_webhooks_has_somewhere_to_put_the_secret(self, code: str) -> None:
        """Поля, которого нет в описании, кабинет не покажет и скрипт
        не спросит — задать секрет будет негде.

        Проверка односторонняя намеренно. Отсутствие поля у перевозчика
        с вебхуками — настоящая ошибка: события молча перестанут приниматься,
        и выглядеть это будет как «перевозчик их не шлёт». Обратное же
        (поле есть, вебхуков нет) ошибкой считать нельзя, пока возможности
        перевозчика не прочитаны по источнику: у Major Express они объявлены
        по матрице ТЗ, а не по WSDL, которого у нас ещё нет.
        """
        schema = schema_for(code)
        assert schema is not None
        if _SUPPORTS_WEBHOOKS[code]:
            assert CREDENTIAL_FIELD in schema.names

    def test_pecom_is_not_asked_for_a_secret_it_cannot_use(self) -> None:
        """У ПЭК вебхуков нет ни в одном из 18 разделов документации.

        Спрашивать у оператора секрет подписи там, где подписывать нечего,
        значит просить данные, которым неоткуда взяться, — и создавать
        впечатление, что вебхуки просто не настроены.
        """
        schema = schema_for("pecom")
        assert schema is not None
        assert CREDENTIAL_FIELD not in schema.names
        assert PecomAdapter.capabilities.supports_webhooks is False

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
        assert missing_fields("yandex", {}) == []

    def test_pochta_accepts_either_the_ready_key_or_the_pair(self) -> None:
        """У Почты ключ авторизации пользователя равносилен паре логин-пароль.

        Список обязательных полей этого не выражает: с одним токеном учётная
        запись прошла бы проверку состава и упала бы на первом же расчёте —
        то есть ошибка нашлась бы у клиента, а не в кабинете.
        """
        assert missing_fields("pochta", {"token": "t", "user_key": "k"}) == []
        assert missing_fields("pochta", {"token": "t", "login": "l", "password": "p"}) == []
        # Половина пары — это не способ: без пароля ключ не собрать.
        assert missing_fields("pochta", {"token": "t", "login": "l"}) == ["user_key"]
        assert missing_fields("pochta", {"token": "t"}) == ["user_key"]
        assert missing_fields("pochta", {}) == ["token", "user_key"]
