"""Массовые отправления: прогон списка через Decision Engine (ADR-0022).

Модуль ничего не считает сам. Он выстраивает уже написанное в очередь:
на каждую строку — расчёт (`rating`), рекомендация и решение (`routing`),
создание отправления (`shipments`). Своих цифр не хранит, поэтому пересчёт
задним числом невозможен by construction.

Три правила, которые здесь важнее остального.

**Частичный успех — нормальное состояние.** Строка, которая не прошла, гасит
себя, а не прогон: у неё статус `failed` и записанная причина. Прогон из 500
строк, где три не прошли, — не сбой прогона, и оператору нужно видеть именно
это, а не «ошибка».

**Идемпотентность выводится, а не запрашивается.** Ключ строки —
`bulk:{run_id}:{row_id}`, детерминированный. Повторный запуск того же прогона
не создаёт вторых решений и вторых заказов: обе нижележащие службы уже умеют
возвращать готовый результат по тому же ключу.

**Ошибка перевозчика не даёт 500.** Она становится причиной отказа строки,
как и любая другая ошибка домена.
"""

from __future__ import annotations

from decimal import ROUND_CEILING
from uuid import UUID

from aerogram.bulk.importing import ImportedRow, parse_recipients
from aerogram.bulk.models import BulkRow, BulkRun
from aerogram.bulk.repository import BulkRepository
from aerogram.bulk.schemas import (
    BulkImportIn,
    BulkImportMatchOut,
    BulkImportOptionOut,
    BulkImportOut,
    BulkImportRowOut,
    BulkRowOut,
    BulkRunCreateIn,
    BulkRunOut,
    BulkRunPage,
)
from aerogram.core.models import Address, Counterparty
from aerogram.core.repository import CounterpartyRepository
from aerogram.rating.schemas import RateRequestIn
from aerogram.rating.service import RateShoppingService
from aerogram.routing.schemas import DecisionRequestIn, RoutingRequestIn
from aerogram.routing.service import DecisionService, RecommendationService
from aerogram.shared.clock import utcnow
from aerogram.shared.enums import (
    BulkImportStatus,
    BulkRowStatus,
    BulkRunStatus,
    RoutingStrategy,
)
from aerogram.shared.errors import AerogramError, NotFound
from aerogram.shared.logging import get_logger
from aerogram.shared.money import Money
from aerogram.shared.schemas import AddressSchema, MoneySchema
from aerogram.shipments.schemas import CreateShipmentRequest
from aerogram.shipments.service import ShipmentService

__all__ = ["BulkService", "NothingToSelectError", "row_idempotency_key"]

log = get_logger(__name__)


class NothingToSelectError(Exception):
    """Строка не может идти дальше, и причина не в сбое.

    Отдельный тип, потому что это не ошибка перевозчика и не ошибка домена:
    расчёт прошёл, просто выбирать оказалось не из чего. В ленту строки
    попадает тот же понятный текст, что и у прочих отказов.
    """


def row_idempotency_key(run_id: UUID, row_id: UUID) -> str:
    """Ключ идемпотентности строки.

    Детерминированный, поэтому повтор всего прогона безопасен: и решение,
    и отправление вернутся готовыми, а не создадутся вторыми.
    """
    return f"bulk:{run_id}:{row_id}"


class BulkService:
    """Прогон списка получателей через расчёт, решение и оформление."""

    def __init__(
        self,
        repository: BulkRepository,
        rating: RateShoppingService,
        recommendations: RecommendationService,
        decisions: DecisionService,
        shipments: ShipmentService,
    ) -> None:
        self._repo = repository
        self._rating = rating
        self._recommendations = recommendations
        self._decisions = decisions
        self._shipments = shipments
        # Адресная книга нужна только импорту, и только на чтение: подбор
        # ищет контрагента по ИНН или названию и берёт его адрес.
        self._counterparties = CounterpartyRepository(repository.session)

    # --- Создание ---------------------------------------------------------

    async def create(
        self, payload: BulkRunCreateIn, *, tenant_id: UUID, user_id: UUID | None
    ) -> BulkRunOut:
        """Создать черновик прогона со списком получателей."""
        run = BulkRun(
            tenant_id=tenant_id,
            user_id=user_id,
            name=payload.name or self._default_name(),
            status=BulkRunStatus.DRAFT,
            strategy=payload.strategy,
            sender_snapshot=payload.origin.model_dump(mode="json"),
        )
        self._repo.add_run(run)

        for position, row_in in enumerate(payload.rows, start=1):
            self._repo.add_row(
                BulkRow(
                    tenant_id=tenant_id,
                    run=run,
                    position=position,
                    recipient_snapshot=row_in.destination.model_dump(mode="json"),
                    cargo_snapshot={
                        "packages": [p.model_dump(mode="json") for p in row_in.packages],
                        "cargo_value": row_in.cargo_value.model_dump(mode="json"),
                        "cargo_type": row_in.cargo_type.value,
                        "deadline": row_in.deadline.isoformat() if row_in.deadline else None,
                    },
                    status=BulkRowStatus.NEW,
                )
            )
        await self._repo.flush()
        log.info("bulk.created", run_id=str(run.id), rows=len(payload.rows))
        return await self._to_out(run, tenant_id=tenant_id)

    @staticmethod
    def _default_name() -> str:
        """Имя по умолчанию — дата, как в кабинете. Правится вручную."""
        return f"Массовый расчёт от {utcnow():%d.%m.%Y}"

    # --- Импорт списка ----------------------------------------------------

    async def import_rows(self, payload: BulkImportIn, *, tenant_id: UUID) -> BulkImportOut:
        """Разобрать список и подобрать получателей по адресной книге.

        Прогон **не создаётся**: оператор видит, что распозналось, что нашлось
        и что нашлось неоднозначно, и только потом создаёт расчёт обычным
        путём — из тех строк, которые готовы. Создавать прогон прямо здесь
        значило бы молча решать за оператора, какой из двух адресов
        контрагента брать.

        «Файл поиска», как у catapulto: строка с ИНН или названием
        подбирается по собственной адресной книге тенанта. Город, если он
        назван в строке, сужает выбор среди адресов найденного контрагента.
        """
        parsed = parse_recipients(payload.text)
        rows: list[BulkImportRowOut] = []
        for row in parsed.rows:
            rows.append(await self._import_one(row))

        counts: dict[str, int] = {status.value: 0 for status in BulkImportStatus}
        for out in rows:
            counts[out.status.value] += 1
        # В логе только счётчики: адреса и названия — персональные данные
        # получателей (CLAUDE.md §6).
        log.info(
            "bulk.imported",
            tenant_id=str(tenant_id),
            rows=len(rows),
            errors=len(parsed.errors),
            tabular=parsed.tabular,
            **counts,
        )
        return BulkImportOut(
            rows=rows, errors=list(parsed.errors), counts=counts, tabular=parsed.tabular
        )

    async def _import_one(self, row: ImportedRow) -> BulkImportRowOut:
        cargo = self._row_cargo(row)
        if not row.has_lookup_key:
            # Адрес назван в самой строке — искать нечего.
            return BulkImportRowOut(
                line=row.line,
                status=BulkImportStatus.PARSED,
                destination=self._destination_from_row(row),
                **cargo,
            )

        if row.inn:
            lookup = f"ИНН {row.inn}"
            found = await self._counterparties.find_by_inn(row.inn)
        else:
            lookup = str(row.name)
            found = await self._counterparties.find_by_name(str(row.name))

        if not found:
            return BulkImportRowOut(
                line=row.line,
                status=BulkImportStatus.NOT_FOUND,
                lookup=lookup,
                message="В адресной книге такого контрагента нет",
                **cargo,
            )
        if len(found) > 1:
            # ИНН общий у головной организации и филиалов; одноимённые
            # контрагенты тоже бывают. Выбирать между ними — оператору.
            return BulkImportRowOut(
                line=row.line,
                status=BulkImportStatus.AMBIGUOUS,
                lookup=lookup,
                message=f"В адресной книге {len(found)} контрагента с таким ключом: "
                + ", ".join(self._describe(c) for c in found[:5]),
                **cargo,
            )

        counterparty = found[0]
        addresses = [a for a in counterparty.addresses if a.deleted_at is None]
        if row.city:
            city = row.city.strip().lower()
            in_city = [a for a in addresses if a.city.strip().lower() == city]
            # Город из строки сужает выбор, но только если он что-то оставил:
            # адрес в другом городе — это не «адресов нет», это вопрос.
            if in_city:
                addresses = in_city

        match = BulkImportMatchOut(
            counterparty_id=counterparty.id,
            counterparty_name=counterparty.name,
            options=[
                BulkImportOptionOut(address_id=a.id, address=self._destination_from_address(a))
                for a in addresses
                if self._address_line(a)
            ],
        )
        usable = [a for a in addresses if self._address_line(a)]
        if not usable:
            return BulkImportRowOut(
                line=row.line,
                status=BulkImportStatus.NOT_FOUND,
                lookup=lookup,
                match=match,
                message="Контрагент найден, но пригодного адреса у него нет: нужны улица и дом",
                **cargo,
            )
        if len(usable) > 1:
            return BulkImportRowOut(
                line=row.line,
                status=BulkImportStatus.AMBIGUOUS,
                lookup=lookup,
                match=match,
                message=f"У контрагента {len(usable)} адреса — выберите нужный",
                **cargo,
            )
        address = usable[0]
        return BulkImportRowOut(
            line=row.line,
            status=BulkImportStatus.RESOLVED,
            lookup=lookup,
            match=match.model_copy(update={"address_id": address.id}),
            destination=self._destination_from_address(address),
            **cargo,
        )

    @staticmethod
    def _describe(counterparty: Counterparty) -> str:
        return f"{counterparty.name} (КПП {counterparty.kpp or '—'})"

    @staticmethod
    def _address_line(address: Address) -> str | None:
        """Строка адреса из полей адресной книги. Пусто — адрес непригоден."""
        parts = [address.street, address.house]
        if address.flat:
            parts.append(f"кв. {address.flat}")
        line = ", ".join(part for part in parts if part)
        return line or None

    def _destination_from_address(self, address: Address) -> AddressSchema:
        return AddressSchema(
            country=address.country_code,
            region=address.region,
            city=address.city,
            postal_code=address.postal_code,
            address_line=self._address_line(address) or "",
        )

    @staticmethod
    def _destination_from_row(row: ImportedRow) -> AddressSchema:
        return AddressSchema(
            country="RU",
            region=row.region,
            city=str(row.city),
            postal_code=row.postal_code,
            address_line=str(row.address_line),
        )

    @staticmethod
    def _row_cargo(row: ImportedRow) -> dict[str, object]:
        """Груз строки в единицах контракта: граммы и копейки.

        Файл называет килограммы и рубли — так пишут люди. Вес округляется
        **вверх** до грамма: вниз занижало бы тариф, а счёт придёт по весу
        перевозчика. Деньги — через ``Money.from_major``, без ``float``.
        """
        cargo: dict[str, object] = {}
        if row.weight_kg is not None:
            grams = (row.weight_kg * 1000).to_integral_value(rounding=ROUND_CEILING)
            cargo["weight_grams"] = max(int(grams), 1)
        if row.value_rub is not None:
            cargo["cargo_value"] = MoneySchema.of(Money.from_major(row.value_rub, "RUB"))
        return cargo

    # --- Чтение -----------------------------------------------------------

    async def get(self, run_id: UUID, *, tenant_id: UUID) -> BulkRunOut:
        run = await self._require(run_id, tenant_id=tenant_id)
        return await self._to_out(run, tenant_id=tenant_id)

    async def page(self, *, tenant_id: UUID, limit: int, offset: int) -> BulkRunPage:
        runs, total = await self._repo.page(tenant_id=tenant_id, limit=limit, offset=offset)
        items = [await self._to_out(run, tenant_id=tenant_id, with_rows=False) for run in runs]
        return BulkRunPage(items=items, total=total)

    async def rename(self, run_id: UUID, name: str, *, tenant_id: UUID) -> BulkRunOut:
        run = await self._require(run_id, tenant_id=tenant_id)
        run.name = name
        return await self._to_out(run, tenant_id=tenant_id)

    # --- Прогон -----------------------------------------------------------

    async def quote_all(self, run_id: UUID, *, tenant_id: UUID, user_id: UUID | None) -> BulkRunOut:
        """Посчитать все строки.

        Строка, по которой расчёт не получился, гасит себя, а не прогон.
        """
        run = await self._require(run_id, tenant_id=tenant_id)
        run.status = BulkRunStatus.QUOTING
        rows = await self._repo.rows_of(run_id, tenant_id=tenant_id)

        for row in rows:
            # Сравнение по значению, а не по тождеству: колонка строковая,
            # и из базы статус возвращается строкой, а не членом перечисления.
            if row.status != BulkRowStatus.NEW:
                continue
            try:
                request = self._rate_request(run, row)
                response = await self._rating.quote(request, tenant_id=tenant_id, user_id=user_id)
            except Exception as exc:
                self._fail(row, exc)
                continue
            row.rate_quote_id = response.quote_id
            row.status = BulkRowStatus.QUOTED

        run.status = await self._run_status(
            run_id, tenant_id=tenant_id, when_busy=BulkRunStatus.QUOTED
        )
        return await self._to_out(run, tenant_id=tenant_id)

    async def select_all(
        self, run_id: UUID, *, tenant_id: UUID, user_id: UUID | None
    ) -> BulkRunOut:
        """Построить рекомендацию и принять решение по каждой посчитанной строке.

        Решение принимается автоматически по стратегии прогона. Персональная
        замена тарифа — это обычный `override` через `POST /v1/decisions`,
        и отдельного пути для неё здесь нет намеренно (ADR-0022).
        """
        run = await self._require(run_id, tenant_id=tenant_id)
        rows = await self._repo.rows_of(run_id, tenant_id=tenant_id)

        for row in rows:
            if row.status != BulkRowStatus.QUOTED or row.rate_quote_id is None:
                continue
            try:
                recommendation = await self._recommendations.recommend(
                    RoutingRequestIn(quote_id=row.rate_quote_id, strategy=self._strategy(run)),
                    tenant_id=tenant_id,
                )
                if recommendation.recommended_offer_id is None:
                    # Рекомендовать нечего: ни одно предложение не подошло —
                    # например, ни один перевозчик не уложился в срок строки.
                    # Это законный исход расчёта, а не сбой, но строка дальше
                    # не идёт, и оператор должен видеть почему.
                    raise NothingToSelectError("Ни одно предложение не подошло под условия строки")
                decision = await self._decisions.decide(
                    DecisionRequestIn(
                        recommendation_id=recommendation.id,
                        selected_offer_id=recommendation.recommended_offer_id,
                    ),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    idempotency_key=row_idempotency_key(run.id, row.id),
                )
            except Exception as exc:
                self._fail(row, exc)
                continue
            row.recommendation_id = recommendation.id
            row.decision_id = decision.decision_id
            row.status = BulkRowStatus.SELECTED

        run.status = await self._run_status(
            run_id, tenant_id=tenant_id, when_busy=BulkRunStatus.QUOTED
        )
        return await self._to_out(run, tenant_id=tenant_id)

    async def create_all(
        self, run_id: UUID, *, tenant_id: UUID, user_id: UUID | None
    ) -> BulkRunOut:
        """Оформить отправления по всем выбранным строкам."""
        run = await self._require(run_id, tenant_id=tenant_id)
        run.status = BulkRunStatus.CREATING
        rows = await self._repo.rows_of(run_id, tenant_id=tenant_id)

        for row in rows:
            if row.status != BulkRowStatus.SELECTED or row.decision_id is None:
                continue
            try:
                shipment = await self._shipments.create(
                    CreateShipmentRequest(decision_id=row.decision_id),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    idempotency_key=row_idempotency_key(run.id, row.id),
                )
            except Exception as exc:
                self._fail(row, exc)
                continue
            row.shipment_id = shipment.id
            row.status = BulkRowStatus.CREATED

        run.status = await self._run_status(
            run_id, tenant_id=tenant_id, when_busy=BulkRunStatus.CREATING
        )
        return await self._to_out(run, tenant_id=tenant_id)

    # --- Внутреннее -------------------------------------------------------

    @staticmethod
    def _fail(row: BulkRow, exc: Exception) -> None:
        """Погасить строку с внятной причиной.

        Ловится **любое** исключение, и это осознанно. Прогон обрабатывает
        сотни строк; неожиданная ошибка на одной из них не должна уносить
        работу по остальным — иначе оператор теряет весь список из-за одного
        неверного адреса или одного сломанного адаптера. Ошибка при этом
        не проглатывается: ожидаемая пишется в лог как событие, неожиданная —
        с трассировкой, чтобы её было видно в разборе.

        Причина обязательна: строка со статусом ``failed`` без объяснения
        выглядит в кабинете как «что-то произошло, но что — неизвестно».
        Это же требует ограничение в схеме.
        """
        expected = isinstance(exc, AerogramError | NothingToSelectError)
        row.status = BulkRowStatus.FAILED
        row.error_message = str(exc) or exc.__class__.__name__
        if expected:
            log.info("bulk.row_failed", row_id=str(row.id), reason=row.error_message[:200])
        else:
            log.exception("bulk.row_crashed", row_id=str(row.id))

    @staticmethod
    def _strategy(run: BulkRun) -> RoutingStrategy:
        """Стратегия прогона.

        Приводится к члену перечисления явно: колонка строковая, и из базы
        значение возвращается строкой.
        """
        return RoutingStrategy(run.strategy) if run.strategy else RoutingStrategy.OPTIMAL

    def _rate_request(self, run: BulkRun, row: BulkRow) -> RateRequestIn:
        """Собрать запрос расчёта из общего отправителя и строки списка."""
        cargo = row.cargo_snapshot
        return RateRequestIn.model_validate(
            {
                "origin": run.sender_snapshot,
                "destination": row.recipient_snapshot,
                "packages": cargo.get("packages"),
                "cargo_value": cargo.get("cargo_value"),
                "cargo_type": cargo.get("cargo_type"),
                "deadline": cargo.get("deadline"),
                "strategy": self._strategy(run).value,
            }
        )

    async def _require(self, run_id: UUID, *, tenant_id: UUID) -> BulkRun:
        run = await self._repo.get_run(run_id, tenant_id=tenant_id)
        if run is None:
            # Чужой прогон по прямому идентификатору — 404, а не 403
            # (CLAUDE.md §6): иначе существование чужих данных подтверждается.
            raise NotFound("Массовый расчёт не найден")
        return run

    async def _run_status(
        self, run_id: UUID, *, tenant_id: UUID, when_busy: BulkRunStatus
    ) -> BulkRunStatus:
        """Состояние прогона по состоянию строк.

        `completed` — когда в работе не осталось ни одной строки, даже если
        часть строк не прошла. `failed` — только когда не прошли все.

        Сброс в базу выполняется здесь, а не у каждого вызывающего: счётчики
        считаются запросом, а запрос не видит изменений, оставшихся в сессии.
        Забыть его в одном из трёх мест было бы легко, и ошибка выглядела бы
        как «прогон не двигается».
        """
        await self._repo.flush()
        unfinished = await self._repo.unfinished_count(run_id, tenant_id=tenant_id)
        if unfinished:
            return when_busy
        counts = await self._repo.counts_by_status(run_id, tenant_id=tenant_id)
        failed = counts.get(BulkRowStatus.FAILED.value, 0)
        total = sum(counts.values())
        return BulkRunStatus.FAILED if total and failed == total else BulkRunStatus.COMPLETED

    async def _to_out(self, run: BulkRun, *, tenant_id: UUID, with_rows: bool = True) -> BulkRunOut:
        counts = await self._repo.counts_by_status(run.id, tenant_id=tenant_id)
        rows = await self._repo.rows_of(run.id, tenant_id=tenant_id) if with_rows else []
        return BulkRunOut(
            id=run.id,
            name=run.name,
            status=run.status,
            strategy=run.strategy,
            sender_snapshot=run.sender_snapshot,
            created_at=run.created_at,
            updated_at=run.updated_at,
            rows=[BulkRowOut.model_validate(row) for row in rows],
            counts=counts,
        )
