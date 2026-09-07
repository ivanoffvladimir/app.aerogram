"""Слияние PDF для пакетной печати.

Проверяется на настоящих файлах, а не на моках: ошибка здесь обнаруживается
пачкой испорченных этикеток на складе, и «функция была вызвана» этого
не ловит.
"""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader, PdfWriter

from aerogram.documents.merge import merge_pdfs


def pdf(pages: int) -> bytes:
    """Настоящий PDF заданного числа страниц."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestMerge:
    def test_pages_add_up(self) -> None:
        result = merge_pdfs([pdf(1), pdf(2), pdf(3)])

        assert result.merged == 3
        assert result.skipped == 0
        assert result.page_count == 6
        assert len(PdfReader(BytesIO(result.content)).pages) == 6

    def test_the_result_is_a_readable_pdf(self) -> None:
        """Склеенный файл уходит на принтер: нечитаемый там не заметят
        до самой печати."""
        content = merge_pdfs([pdf(1), pdf(1)]).content
        assert content.startswith(b"%PDF")

    def test_the_order_is_kept(self) -> None:
        """Пачка на складе раскладывается вместе со списком прогона:
        перестановка означала бы наклеенную не ту этикетку."""
        first, second = pdf(1), pdf(2)
        forward = merge_pdfs([first, second])
        backward = merge_pdfs([second, first])

        # Порядок различим по числу страниц до и после стыка: обратный
        # порядок даёт другую разбивку.
        assert len(PdfReader(BytesIO(forward.content)).pages) == 3
        assert forward.content != backward.content


class TestBrokenParts:
    def test_a_broken_file_is_skipped_not_fatal(self) -> None:
        """Одна битая форма из ста не должна оставлять кладовщика
        без девяноста девяти."""
        result = merge_pdfs([pdf(1), b"not a pdf at all", pdf(1)])

        assert result.merged == 2
        assert result.skipped == 1
        assert result.page_count == 2

    def test_how_many_were_skipped_is_reported(self) -> None:
        """Молча потерять этикетку хуже, чем не склеить вовсе: кладовщик
        должен знать, что в пачке не всё."""
        assert merge_pdfs([b"", b"broken"]).skipped == 2

    def test_nothing_readable_yields_an_empty_pack(self) -> None:
        """Пустой результат не скрывается: решение, что с ним делать,
        принимает вызывающий, и он отвечает отказом."""
        result = merge_pdfs([b"broken"])

        assert result.merged == 0
        assert result.page_count == 0
