"""Заглушка Major Express: контракт соблюдён, отказ внятен.

Смысл файла — не «проверить нереализованное», а зафиксировать два свойства,
которые легко потерять при написании настоящего адаптера:

* заглушка реализует ``CarrierAdapter`` целиком, поэтому её можно
  зарегистрировать, не ломая расчёт;
* «не настроено» отличается от «временно недоступен»: во втором случае
  оператору предлагают повторить, в первом повторять бесполезно.
"""

from __future__ import annotations

import pytest

from aerogram.carriers.base import CarrierAccount, CarrierAdapter
from aerogram.carriers.credentials import (
    CREDENTIAL_SCHEMAS,
    PENDING_CARRIERS,
    missing_fields,
    schema_for,
)
from aerogram.carriers.major import MajorExpressAdapter
from aerogram.carriers.major.client import wsdl_path
from aerogram.rating.service import RETRYABLE_FAILURES
from aerogram.shared.enums import LabelFormat
from aerogram.shared.errors import CarrierNotConfigured


def account(**credentials: str) -> CarrierAccount:
    return CarrierAccount(
        account_id="00000000-0000-0000-0000-000000000001",
        carrier_code="major",
        mode="own_contract",
        credentials=dict(credentials),
    )


class TestContract:
    def test_the_stub_satisfies_the_adapter_protocol(self) -> None:
        """Иначе её нельзя будет зарегистрировать, не тронув реестр."""
        assert isinstance(MajorExpressAdapter(), CarrierAdapter)

    def test_capabilities_are_declared_from_the_carrier_matrix(self) -> None:
        """Кабинет показывает клиенту, чего ждать, ещё до реализации."""
        caps = MajorExpressAdapter().capabilities
        assert caps.supports_cancel
        assert LabelFormat.PDF_A4 in caps.supported_label_formats


class TestRefusal:
    @pytest.mark.asyncio
    async def test_missing_password_is_named(self) -> None:
        """Названо недостающее поле — иначе оператору нечего исправлять."""
        with pytest.raises(CarrierNotConfigured) as exc:
            await MajorExpressAdapter().quote(None, account(login="l"))  # type: ignore[arg-type]
        assert "password" in str(exc.value)

    @pytest.mark.asyncio
    async def test_the_password_itself_never_reaches_the_message(self) -> None:
        """Сообщение уходит в ответ API и в лог: значения там быть не должно."""
        with pytest.raises(CarrierNotConfigured) as exc:
            await MajorExpressAdapter().create(None, account(login="l", password=""))  # type: ignore[arg-type]
        assert "l" not in str(exc.value).split(": ")[-1]

    @pytest.mark.asyncio
    async def test_complete_credentials_still_lack_the_wsdl(self) -> None:
        """Пока WSDL нет, отказ обязан называть именно его."""
        assert wsdl_path() is None, "появился WSDL — заглушку пора заменять адаптером"
        with pytest.raises(CarrierNotConfigured) as exc:
            await MajorExpressAdapter().create(None, account(login="l", password="p"))  # type: ignore[arg-type]
        assert "WSDL" in str(exc.value)

    @pytest.mark.asyncio
    async def test_ghost_reconciliation_refuses_like_the_rest(self) -> None:
        """``find_by_number`` не должен тихо возвращать None.

        None означает «у перевозчика такого заказа нет», и сверка «призраков»
        сочла бы ненастроенную интеграцию доказательством отсутствия заказа.
        """
        with pytest.raises(CarrierNotConfigured):
            await MajorExpressAdapter().find_by_number("AG-1", account(login="l", password="p"))

    def test_not_configured_is_not_retryable(self) -> None:
        """Повторять бесполезно: нужны действия администратора, а не время."""
        assert CarrierNotConfigured.code not in RETRYABLE_FAILURES


class TestCredentialSchemas:
    def test_declared_carriers_name_their_required_fields(self) -> None:
        """Обязательное — то, без чего перевозчик не работает вовсе.

        Секрет подписи вебхуков сюда не входит намеренно: без него расчёт,
        оформление и трекинг опросом работают, и требовать его значило бы
        не дать подключиться тому, кому вебхуки не нужны.
        """
        assert schema_for("major").required_names == ("login", "password")  # type: ignore[union-attr]
        assert schema_for("cdek").required_names == (  # type: ignore[union-attr]
            "client_id",
            "client_secret",
        )

    def test_every_carrier_also_asks_for_the_webhook_secret(self) -> None:
        """Поля, которого нет в описании, кабинет не покажет и скрипт
        не спросит — задать его будет негде."""
        for code in ("major", "cdek"):
            assert "webhook_secret" in schema_for(code).names  # type: ignore[union-attr]

    def test_identifiers_are_not_hidden_but_secrets_are(self) -> None:
        """Скрывать идентификатор клиента значит мешать оператору себя проверить."""
        fields = {f.name: f.secret for f in CREDENTIAL_SCHEMAS["cdek"].fields}
        assert fields == {"client_id": False, "client_secret": True, "webhook_secret": True}

    def test_missing_fields_lists_what_to_fill(self) -> None:
        assert missing_fields("major", {"login": "l"}) == ["password"]
        assert missing_fields("major", {"login": "l", "password": "p"}) == []

    def test_an_unknown_carrier_is_not_silently_approved(self) -> None:
        """Пустой список у неизвестного ТК — «нечем проверить», а не «всё в порядке».

        Поэтому такой перевозчик обязан числиться в ``PENDING_CARRIERS``:
        иначе состав его доступов забудут объявить вместе с адаптером.
        """
        assert schema_for("yandex") is None
        assert missing_fields("yandex", {}) == []
        assert set(PENDING_CARRIERS) == {"yandex"}

    def test_dellin_is_declared_now_that_its_adapter_exists(self) -> None:
        """Состав доступов объявляется вместе с адаптером, в том же коммите.

        У Деловых Линий он сверен по официальной OpenAPI (ADR-0020): ключ
        приложения обязателен, вход в кабинет — нет, потому что без него
        расчёт возвращает публичный тариф, а не падает.
        """
        schema = schema_for("dellin")
        assert schema is not None
        assert "dellin" not in PENDING_CARRIERS
        assert missing_fields("dellin", {}) == ["appkey"]
        assert missing_fields("dellin", {"appkey": "k"}) == []

    def test_pecom_asks_for_the_cabinet_login_and_an_api_key(self) -> None:
        """Basic-аутентификация: логин личного кабинета и ключ API.

        Логин не секрет — скрывать его значит мешать оператору проверить,
        что он ввёл. Ключ секрет: он и есть пароль доступа к API.
        """
        schema = schema_for("pecom")
        assert schema is not None
        assert schema.required_names == ("login", "api_key")
        assert missing_fields("pecom", {"login": "user"}) == ["api_key"]
        assert [f.secret for f in schema.fields] == [False, True]

    def test_pochta_asks_for_an_application_token_and_a_user_key(self) -> None:
        """Авторизация «Отправки» двухсоставная и без единого сетевого вызова.

        Токен приложения обязателен: без него не работает ничего. Ключ
        пользователя обязателен по смыслу, но не по списку — его можно
        не вводить, а дать пару логин-пароль, из которой он собирается,
        и это выражено через ``any_of``.
        """
        schema = schema_for("pochta")
        assert schema is not None
        assert "pochta" not in PENDING_CARRIERS
        assert schema.required_names == ("token",)
        # Вебхуков Почта не присылает — секрета подписи у неё нет.
        assert "webhook_secret" not in schema.names

    def test_declared_and_pending_sets_do_not_overlap(self) -> None:
        assert not set(CREDENTIAL_SCHEMAS) & PENDING_CARRIERS
