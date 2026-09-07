"""Слияние готовых PDF в один файл для пакетной печати.

Чистая функция над байтами: ни базы, ни сети, ни хранилища. Так её можно
проверить на настоящих файлах, а не на моках, — а пакетная печать это ровно
тот случай, когда ошибка обнаруживается пачкой испорченных этикеток
на складе.

Мы ничего не рисуем — только склеиваем готовое. Формы перевозчиков приходят
собранными, и генерации PDF в проекте нет (ADR-0016).
"""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO

from pypdf import PdfWriter
from pypdf.errors import PyPdfError

from aerogram.shared.logging import get_logger

__all__ = ["MergeResult", "merge_pdfs"]

log = get_logger(__name__)


class MergeResult:
    """Склеенный файл и число страниц в нём.

    Число страниц нужно кабинету: «шестьдесят страниц» отвечает кладовщику
    на вопрос, что он сейчас отправит на принтер, а размер в килобайтах —
    нет.
    """

    __slots__ = ("content", "merged", "page_count", "skipped")

    def __init__(self, content: bytes, page_count: int, merged: int, skipped: int) -> None:
        self.content = content
        self.page_count = page_count
        #: Сколько файлов вошло в пачку и сколько отброшено как нечитаемые.
        self.merged = merged
        self.skipped = skipped


def merge_pdfs(parts: Sequence[bytes]) -> MergeResult:
    """Склеить PDF по порядку.

    Порядок сохраняется: пачка на складе раскладывается вместе со списком
    прогона, и перестановка страниц означала бы наклеенную не ту этикетку.

    **Нечитаемый файл пропускается, а не роняет пачку.** Одна битая форма
    из ста не должна оставлять кладовщика без девяноста девяти; сколько
    пропущено, возвращается числом и показывается человеку — молча потерять
    этикетку хуже, чем не склеить вовсе.
    """
    writer = PdfWriter()
    merged = 0
    skipped = 0
    for part in parts:
        try:
            writer.append(BytesIO(part))
        except (PyPdfError, ValueError, OSError) as error:
            # Ловим широко: перевозчик мог прислать обрезанный файл, файл
            # чужого формата или защищённый паролем. Разница между видами
            # поломки здесь не важна — важно не потерять остальные.
            skipped += 1
            log.warning("documents.merge_skipped", error_type=type(error).__name__)
            continue
        merged += 1

    buffer = BytesIO()
    writer.write(buffer)
    writer.close()
    return MergeResult(buffer.getvalue(), len(writer.pages), merged, skipped)
