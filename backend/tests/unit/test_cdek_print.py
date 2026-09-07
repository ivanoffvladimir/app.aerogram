"""Печатная форма СДЭК: ШК-место (неделя 7).

Фикстуры синтетические — см. tests/fixtures/cdek/README.md. Структура сверена
с ``src/Actions/Barcodes.php`` и ``src/BaseTypes/Barcode.php`` официального
SDK, но прогона на боевом контуре не заменяет.

Что здесь дорого ошибиться. Этикетка едет на склад и клеится на коробку:
отдать вместо неё тело отказа значит отправить груз с нечитаемой наклейкой,
а объявить готовой неготовую форму — то же самое. Отдельно проверяется, что
в каталог накладных попадает номер СДЭК, а не идентификатор заявки на печать:
второй живёт час и уже назавтра не значит ничего (ADR-0030).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from aerogram.carriers.base import CarrierAccount
from aerogram.carriers.cdek.adapter import CdekAdapter
from aerogram.carriers.cdek.client import SANDBOX_BASE_URL, CdekClient
from aerogram.carriers.cdek.print_forms import barcode_payload, is_ready, waybill_number
from aerogram.shared.enums import LabelFormat
from aerogram.shared.errors import CarrierError, CarrierValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cdek"

ORDER_UUID = "d1d9a0f4-1b7c-4e2f-9a83-5f6c7d8e9a0b"
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\ntest label\n%%EOF\n"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


class Contour:
    """Поддельный контур СДЭК, различающий три вызова печати.

    Различать их обязательно: весь смысл реализации в том, что заказ формы,
    опрос готовности и скачивание файла — три РАЗНЫХ обращения. Заглушка,
    отвечающая одинаково на всё, пропустила бы перепутанные пути.
    """

    def __init__(self, *, status_body: dict[str, Any], file_body: bytes | None = PDF) -> None:
        self.status_body = status_body
        self.file_body = file_body
        self.paths: list[str] = []
        self.created: dict[str, Any] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "oauth" in path:
            return httpx.Response(200, json=load("oauth_ok"))
        self.paths.append(f"{request.method} {path}")
        if path.endswith(".pdf"):
            if self.file_body is None:
                return httpx.Response(
                    200,
                    json={"errors": [{"code": "v2_print_form_not_ready", "message": "Не готово"}]},
                )
            return httpx.Response(
                200, content=self.file_body, headers={"content-type": "application/pdf"}
            )
        if request.method == "POST":
            self.created = json.loads(request.content)
            return httpx.Response(200, json=load("print_barcode_accepted"))
        return httpx.Response(200, json=self.status_body)


def _adapter(contour: Contour) -> CdekAdapter:
    def factory(_: CarrierAccount) -> CdekClient:
        inner = httpx.AsyncClient(
            transport=httpx.MockTransport(contour.handler), base_url=SANDBOX_BASE_URL
        )
        return CdekClient(client_id="i", client_secret="s", http_client=inner)

    return CdekAdapter(client_factory=factory)


ACCOUNT = CarrierAccount(
    account_id="1",
    carrier_code="cdek",
    mode="own_contract",
    credentials={"client_id": "i", "client_secret": "s"},
)


class TestPayload:
    """Тело запроса. Проверяется без сети: это чистая функция."""

    def test_order_is_addressed_by_uuid(self) -> None:
        # Номер СДЭК появляется не сразу после создания заказа,
        # а идентификатор известен всегда — адресоваться надо по нему.
        payload = barcode_payload(ORDER_UUID, LabelFormat.PDF_A6)
        assert payload["orders"] == [{"order_uuid": ORDER_UUID}]

    @pytest.mark.parametrize(
        ("fmt", "code"),
        [
            (LabelFormat.PDF_A4, "A4"),
            (LabelFormat.PDF_A5, "A5"),
            (LabelFormat.PDF_A6, "A6"),
        ],
    )
    def test_format_reaches_carrier(self, fmt: LabelFormat, code: str) -> None:
        assert barcode_payload(ORDER_UUID, fmt)["format"] == code

    def test_one_copy_by_default(self) -> None:
        # Две копии — это квитанция, у неё другой путь. Этикетка нужна одна:
        # лишняя копия на складе клеится на вторую коробку по ошибке.
        assert barcode_payload(ORDER_UUID, LabelFormat.PDF_A4)["copy_count"] == 1


class TestReadiness:
    """Готовность формы. Ошибка в любую сторону дорога по-своему."""

    def test_accepted_is_not_ready(self) -> None:
        assert is_ready(load("print_barcode_accepted")) is False

    def test_successful_is_ready(self) -> None:
        assert is_ready(load("print_barcode_ready")) is True

    def test_url_alone_is_enough(self) -> None:
        # Ссылка на файл есть — значит файл есть, каким бы ни было состояние
        # заявки. Признака два, и достаточно любого.
        assert is_ready({"entity": {"url": "https://example.invalid/x.pdf"}}) is True

    def test_empty_answer_is_not_ready(self) -> None:
        # Пустой ответ — это «неизвестно», и трактовать его как готовность
        # значит пойти качать несуществующий файл.
        assert is_ready({}) is False


class TestWaybillNumber:
    def test_number_comes_from_order(self) -> None:
        assert waybill_number(load("print_barcode_ready")) == "1106207152"

    def test_absent_number_is_none(self) -> None:
        assert waybill_number(load("print_barcode_accepted")) is None


@pytest.mark.asyncio
class TestLabel:
    async def test_ready_form_returns_file(self) -> None:
        contour = Contour(status_body=load("print_barcode_ready"))
        result = await _adapter(contour).label(ORDER_UUID, LabelFormat.PDF_A6, ACCOUNT)
        assert result.is_pending is False
        assert result.content == PDF
        assert result.format is LabelFormat.PDF_A6

    async def test_three_calls_in_order(self) -> None:
        contour = Contour(status_body=load("print_barcode_ready"))
        await _adapter(contour).label(ORDER_UUID, LabelFormat.PDF_A4, ACCOUNT)
        uuid = load("print_barcode_accepted")["entity"]["uuid"]
        assert contour.paths == [
            "POST /v2/print/barcodes",
            f"GET /v2/print/barcodes/{uuid}",
            f"GET /v2/print/barcodes/{uuid}.pdf",
        ]

    async def test_waybill_number_is_the_cdek_number(self) -> None:
        # Не идентификатор заявки на печать: он живёт час, а номер
        # накладной остаётся в каталоге на пять лет (ADR-0030).
        contour = Contour(status_body=load("print_barcode_ready"))
        result = await _adapter(contour).label(ORDER_UUID, LabelFormat.PDF_A4, ACCOUNT)
        assert result.external_ref == "1106207152"
        assert result.external_ref != load("print_barcode_accepted")["entity"]["uuid"]

    async def test_not_ready_is_pending_without_file(self) -> None:
        contour = Contour(status_body=load("print_barcode_accepted"))
        result = await _adapter(contour).label(ORDER_UUID, LabelFormat.PDF_A4, ACCOUNT)
        assert result.is_pending is True
        assert result.content is None
        # За файлом не ходили: качать нечего.
        assert not any(p.endswith(".pdf") for p in contour.paths)

    async def test_pending_form_does_not_claim_a_waybill(self) -> None:
        # Иначе в каталог накладных попал бы пустой номер от неготовой формы.
        contour = Contour(status_body=load("print_barcode_accepted"))
        result = await _adapter(contour).label(ORDER_UUID, LabelFormat.PDF_A4, ACCOUNT)
        assert result.external_ref is None

    async def test_rejected_request_is_an_error(self) -> None:
        contour = Contour(status_body=load("print_barcode_invalid"))
        with pytest.raises(CarrierValidationError):
            await _adapter(contour).label(ORDER_UUID, LabelFormat.PDF_A4, ACCOUNT)

    async def test_error_body_is_not_served_as_a_label(self) -> None:
        # Тот же путь отдаёт и файл, и JSON с отказом. Отдать второе как
        # этикетку — отправить груз с наклейкой, которая не открывается.
        contour = Contour(status_body=load("print_barcode_ready"), file_body=None)
        with pytest.raises(CarrierError):
            await _adapter(contour).label(ORDER_UUID, LabelFormat.PDF_A4, ACCOUNT)

    async def test_empty_file_is_an_error(self) -> None:
        contour = Contour(status_body=load("print_barcode_ready"), file_body=b"")
        with pytest.raises(CarrierError):
            await _adapter(contour).label(ORDER_UUID, LabelFormat.PDF_A4, ACCOUNT)

    async def test_zpl_is_refused_out_loud(self) -> None:
        # СДЭК термопринтерного формата не предлагает. Подменить его PDF-ом
        # значило бы отдать на принтер файл, который тот не напечатает.
        contour = Contour(status_body=load("print_barcode_ready"))
        with pytest.raises(CarrierValidationError):
            await _adapter(contour).label(ORDER_UUID, LabelFormat.ZPL, ACCOUNT)
        assert contour.paths == []

    async def test_declared_formats_all_work(self) -> None:
        # Возможности адаптера — обещание наружу: каждый объявленный формат
        # обязан доходить до перевозчика, иначе обещание ложное.
        for fmt in CdekAdapter.capabilities.supported_label_formats:
            contour = Contour(status_body=load("print_barcode_ready"))
            result = await _adapter(contour).label(ORDER_UUID, fmt, ACCOUNT)
            assert result.content == PDF
            assert contour.created is not None
            assert (
                contour.created["format"]
                == {"pdf_a4": "A4", "pdf_a5": "A5", "pdf_a6": "A6"}[fmt.value]
            )
