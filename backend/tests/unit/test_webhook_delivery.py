"""Доставка исходящего вебхука: куда именно уходит запрос.

Адрес получателя задаёт клиент, а запрос уходит с нашего сервера. Проверка
адреса покрыта тестами подписки; здесь проверяется то, что проверкой
не закрывается: **подключение идёт к проверенному адресу, а не по имени**.

Разница не теоретическая. Имя, разрешённое второй раз — уже клиентом HTTP, —
может указать в `169.254.169.254`, и проверка, сделанная секундой раньше,
об этом не узнает. Настоящей сети здесь нет: подменяется транспорт,
а проверка адреса остаётся боевой.
"""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from aerogram.shared.errors import ValidationFailed
from aerogram.tracking import outgoing

pytestmark = pytest.mark.asyncio

URL = "https://hooks.client.example/aerogram"
PUBLIC = "93.184.216.34"
INTERNAL = "169.254.169.254"


class Recorder:
    """Транспорт, запоминающий, куда и с чем ушёл запрос."""

    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.seen: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return httpx.Response(self.status)

        return httpx.MockTransport(handle)

    @property
    def last(self) -> httpx.Request:
        assert self.seen, "запрос не уходил"
        return self.seen[-1]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=rec.transport(),
            timeout=outgoing.TIMEOUT_SECONDS,
            follow_redirects=False,
        )

    monkeypatch.setattr(outgoing, "http_client", client)
    return rec


def resolving_to(monkeypatch: pytest.MonkeyPatch, *rounds: list[str]) -> None:
    """Подменить разрешение имени. Каждый вызов отдаёт следующий ответ.

    Несколько ответов нужны затем, чтобы выразить саму подмену DNS: первое
    разрешение честное, второе указывает внутрь. Если запись кончилась,
    последняя повторяется — тесту с одним разрешением незачем это знать.
    """
    answers = list(rounds)

    async def fake(host: str) -> list[str]:
        if host != "hooks.client.example":
            raise socket.gaierror(f"нет записи для {host}")
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(outgoing, "resolve", fake)


async def deliver(**kw: Any) -> int:
    return await outgoing.deliver(URL, "s3cret", "shipment.delivered", {"number": "AG-1"}, **kw)


class TestPinning:
    async def test_the_connection_goes_to_the_validated_address(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        resolving_to(monkeypatch, [PUBLIC])

        assert await deliver() == 204
        assert recorder.last.url.host == PUBLIC, "подключились по имени, а не по адресу"

    async def test_the_host_header_keeps_the_name(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        """IP в ``Host`` дал бы 404 у любого хостинга с несколькими сайтами."""
        resolving_to(monkeypatch, [PUBLIC])
        await deliver()

        assert recorder.last.headers["Host"] == "hooks.client.example"

    async def test_the_certificate_is_checked_against_the_name(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        """SNI — это и выбор сертификата получателем, и наша проверка его.

        Без имени в SNI проверка шла бы против IP и падала бы на любом
        нормальном сертификате, а «починка» отключением проверки была бы
        хуже самой болезни.
        """
        resolving_to(monkeypatch, [PUBLIC])
        await deliver()

        assert recorder.last.extensions["sni_hostname"] == "hooks.client.example"

    async def test_the_path_and_query_survive(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        resolving_to(monkeypatch, [PUBLIC])
        await outgoing.deliver(
            "https://hooks.client.example:8443/aerogram?tenant=7", "s", "shipment.delivered", {}
        )

        assert recorder.last.url.path == "/aerogram"
        assert recorder.last.url.query == b"tenant=7"
        # Порт — часть адреса получателя, и вместе с ним часть ``Host``.
        assert recorder.last.url.port == 8443
        assert recorder.last.headers["Host"] == "hooks.client.example:8443"

    async def test_an_ipv6_answer_is_bracketed(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        """Иначе адрес просто не собирается в URL."""
        resolving_to(monkeypatch, ["2606:2800:220:1:248:1893:25c8:1946"])
        await deliver()

        assert recorder.last.url.host == "2606:2800:220:1:248:1893:25c8:1946"


class TestTheWindowIsClosed:
    async def test_a_second_resolution_cannot_redirect_the_delivery(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        """Ровно подмена DNS: проверка честная, подключение — уже нет.

        Резолвер отдаёт публичный адрес проверке и внутренний — всякому,
        кто спросит после. Подключение по имени ушло бы во второй; наше
        уходит в первый.
        """
        resolving_to(monkeypatch, [PUBLIC], [INTERNAL])

        assert await deliver() == 204
        assert recorder.last.url.host == PUBLIC

    async def test_an_internal_address_is_never_dialled(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        resolving_to(monkeypatch, [INTERNAL])

        with pytest.raises(ValidationFailed):
            await deliver()
        assert recorder.seen == [], "запрос ушёл, хотя адрес отвергнут"

    async def test_one_internal_record_refuses_the_whole_host(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        """Иначе подключение досталось бы той записи, которую выберет система."""
        resolving_to(monkeypatch, [PUBLIC, "10.0.0.7"])

        with pytest.raises(ValidationFailed):
            await deliver()
        assert recorder.seen == []

    async def test_an_empty_resolution_is_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        """Подключаться некуда, а «пропустить» значило бы пойти по имени."""
        resolving_to(monkeypatch, [])

        with pytest.raises(ValidationFailed):
            await deliver()
        assert recorder.seen == []


class TestSignature:
    async def test_the_signature_covers_the_body_that_was_sent(
        self, monkeypatch: pytest.MonkeyPatch, recorder: Recorder
    ) -> None:
        """Подпись считается по тому же телу, что уходит в сеть.

        Разойдись они — получатель отверг бы каждую доставку, и узнали бы мы
        об этом от него, а не от себя.
        """
        resolving_to(monkeypatch, [PUBLIC])
        await deliver()

        request = recorder.last
        assert request.headers[outgoing.SIGNATURE_HEADER] == outgoing.sign(
            "s3cret", request.headers[outgoing.TIMESTAMP_HEADER], request.content
        )
