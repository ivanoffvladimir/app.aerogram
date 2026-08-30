"""Исходящие вебхуки: адрес и подпись (FR-3.6).

Здесь проверяется не «работает ли отправка», а два свойства, ошибка в которых
дороже всего: адрес задаёт клиент, а запрос уходит с нашего сервера, и подпись
— единственное, чем получатель отличает нас от кого угодно.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aerogram.shared.errors import ValidationFailed
from aerogram.tracking.outgoing import (
    WEBHOOK_EVENTS,
    accepted,
    generate_secret,
    sign,
    validate_url,
)
from aerogram.tracking.webhooks import MAX_ATTEMPTS, next_attempt_after


class TestUrlIsNotAWayInside:
    """SSRF: подписавшись на внутренний адрес, клиент простучал бы чужую сеть."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1/hook",
            "https://localhost/hook",
            "https://169.254.169.254/latest/meta-data",
            "https://10.0.0.5/hook",
            "https://192.168.1.1/hook",
            "https://[::1]/hook",
        ],
    )
    def test_internal_addresses_are_refused(self, url: str) -> None:
        with pytest.raises(ValidationFailed) as exc:
            validate_url(url)
        assert exc.value.field == "url"

    def test_the_refusal_does_not_confirm_what_was_found(self) -> None:
        """Незачем подтверждать клиенту, что именно он нащупал внутри."""
        with pytest.raises(ValidationFailed) as exc:
            validate_url("https://169.254.169.254/latest/meta-data")
        assert "169.254" not in str(exc.value)

    @pytest.mark.parametrize("url", ["http://example.com/hook", "ftp://example.com/hook"])
    def test_only_https_is_allowed(self, url: str) -> None:
        """Подпись подтверждает происхождение, но не скрывает содержимое,
        а в теле — номера отправлений клиента."""
        with pytest.raises(ValidationFailed):
            validate_url(url)

    def test_a_name_that_does_not_resolve_is_refused(self) -> None:
        with pytest.raises(ValidationFailed):
            validate_url("https://такого-узла-точно-нет.invalid/hook")


class TestSignature:
    def test_the_same_input_gives_the_same_signature(self) -> None:
        assert sign("s", "1", b"{}") == sign("s", "1", b"{}")

    def test_another_secret_gives_another_signature(self) -> None:
        assert sign("s", "1", b"{}") != sign("другой", "1", b"{}")

    def test_time_is_part_of_the_signature(self) -> None:
        """Иначе старую доставку можно переиграть хоть через год."""
        assert sign("s", "1", b"{}") != sign("s", "2", b"{}")

    def test_the_separator_prevents_a_shifted_collision(self) -> None:
        """Без разделителя «12» + «3…» и «1» + «23…» дали бы одну подпись."""
        assert sign("s", "12", b"3") != sign("s", "1", b"23")

    def test_secrets_are_not_predictable(self) -> None:
        assert len({generate_secret() for _ in range(50)}) == 50


class TestRetries:
    def test_five_attempts_then_giving_up(self) -> None:
        """FR-3.6: пять попыток. Шестая ничего не изменит, а очередь занимает."""
        assert next_attempt_after(MAX_ATTEMPTS) is None
        assert next_attempt_after(MAX_ATTEMPTS - 1) is not None

    def test_the_delay_grows_exponentially(self) -> None:
        """Получатель, лежащий минуту, и лежащий два часа — разные ситуации."""
        delays = [next_attempt_after(n) for n in range(1, MAX_ATTEMPTS)]
        assert delays == [
            timedelta(minutes=1),
            timedelta(minutes=5),
            timedelta(minutes=25),
            timedelta(minutes=125),
        ]


class TestContract:
    def test_the_four_events_of_the_spec(self) -> None:
        assert {
            "shipment.status_changed",
            "shipment.delivered",
            "shipment.exception",
            "shipment.delayed",
        } == WEBHOOK_EVENTS

    def test_any_2xx_counts_as_accepted(self) -> None:
        """Получатель волен ответить 200 или 204 — это его дело."""
        assert accepted(200) and accepted(204)
        assert not accepted(302), "перенаправление — не приём"
        assert not accepted(500)
