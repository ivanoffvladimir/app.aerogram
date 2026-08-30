# Logistics OS — Общее системное ТЗ

Функциональная архитектура, логистика и интеграции • версия 3.0

ФИНАЛЬНАЯ РЕДАКЦИЯ ДЛЯ СТАРТА РАЗРАБОТКИ

## 1. Назначение системы

Документ определяет функциональную модель Logistics OS как логистической информационной системы: процессы, сущности, интеграционный слой, статусную модель, carrier adapters, правила расчета и эксплуатационные требования.

## 2. Контекстная архитектура

ERP / 1С / WMS / CRM клиента → Logistics OS API/UI → Carrier Adapter Layer → API перевозчиков. Внутри Logistics OS: Rate Engine, Routing Engine, Shipment Service, Tracking Service, Carrier Intelligence, Billing Lite, Decision History, Audit/Observability.

## 3. End-to-end процесс

| Шаг | Система | Результат |

|---|---|---|

| 1. Input | UI/API получает маршрут, груз, стоимость, deadline, services. | Shipment draft. |

| 2. Quote | Rate Engine параллельно вызывает adapters. | Нормализованные offers + partial failures. |

| 3. Normalize | Стоимость, ETA, услуги, ограничения приводятся к единой модели. | Total Cost + eligibility. |

| 4. Recommend | Routing Engine применяет hard constraints и strategy. | Recommendation + explanation. |

| 5. Decide | Human/Auto Select подтверждает offer. | Immutable Decision Snapshot. |

| 6. Ship | Shipment Service вызывает create_shipment. | Carrier shipment ID, tracking number, label. |

| 7. Track | Webhook/polling нормализует события. | Unified timeline + exceptions. |

| 8. Close | Delivered + invoice/claim data. | Delivery Outcome. |

| 9. Learn | Carrier Intelligence агрегирует факт. | Score/risk/confidence для следующих решений. |

## 4. Нормализованная статусная модель

| Internal status | Смысл |

|---|---|

| Draft | Черновик. |

| Quoted | Получены тарифы. |

| Created | Создано у перевозчика. |

| PickedUp | Груз принят/забран. |

| InTransit | В пути. |

| OutForDelivery | На последней миле. |

| Delivered | Доставлено. |

| Delayed | Риск/факт нарушения срока. |

| Exception | Проблема, требующая внимания. |

| Cancelled | Отменено. |

Оригинальный carrier status всегда хранится рядом с normalized_status. Маппинг версионируется по adapter_version.

## 5. Carrier Adapter Layer — единый контракт

capabilities() — объявляет поддерживаемые функции.

get_rates() — стоимость/срок/допуслуги.

create_shipment() — создание накладной/заказа.

cancel_shipment() — если поддерживается.

get_tracking() — polling fallback.

get_label()/get_documents() — если поддерживается.

health_check() — техническая доступность.

normalize_error() и normalize_status() — обязательны.

Все внешние вызовы имеют timeout, retry policy, correlation_id и метрики.

## 6. Первые 5 интеграций — capability map

| Перевозчик | Протокол/доступ | Rate | Create | Track | Особенности для MVP |

|---|---|---|---|---|---|

| Major Express | SOAP/WSDL, Basic Auth для клиентских сервисов. | Да: Calculator1 с договором клиента. | Да: CreateWaybill; также LTL методы. | Да, через методы истории/событий. | Отдельно Express и LTL; нужен SOAP adapter и справочники городов. |

| CDEK | Официальная developer platform/API; договорные учетные данные. | Да. | Да. | Да. | Расчет, накладные, статусы, документы; точные v2 schemas проверить при выдаче credentials. |

| Деловые Линии | HTTP API, app key/авторизация по документации. | Да: /v2/calculator. | Да, после подключения клиентского API. | Да. | Сильный LTL use case; учитывать терминал/адрес и допуслуги. |

| ПЭК | Public calculator + API личного кабинета. | Да. | Через API ЛК. | Через API ЛК. | Public API годится для базового расчета; договорные данные требуют authenticated API. |

| Яндекс Доставка | HTTP + Bearer token. | Да: предварительная оценка/offers. | Да: claim/order flow. | Да. | Есть express/same-day и доставка по России; разные host/flow могут требовать отдельных sub-adapters. |

## 7. Стратегия интеграции

Не пытаться унифицировать все carrier-specific поля в Core. Core содержит минимальную общую модель + extension metadata.

Каждый adapter имеет capability flags: rates, create, cancel, tracking, labels, pickup, insurance, COD, terminals, webhook.

Carrier-specific reference data кешируется и версионируется.

Справочники городов/терминалов связываются с internal Location ID через mapping table.

Для клиента может быть несколько CarrierAccount одного перевозчика (разные договоры/филиалы).

Тариф клиента считается только через его CarrierAccount; platform tariff — через отдельный Logistics OS account/source.

## 8. Rate Engine

Fan-out запросов параллельно; общий SLA ответа не должен зависеть от самого медленного перевозчика.

Partial success является нормальным состоянием.

Каждый RateOffer содержит source, service, ETA, cost components, Total Cost, valid_until, raw reference.

Money хранится в minor units + currency.

Quote имеет TTL; просроченный quote нельзя использовать для create без refresh/revalidation.

Insurance и platform-level surcharges рассчитываются после carrier response, но до recommendation.

## 9. Routing Engine

Hard constraints: deadline, carrier allow/deny, cargo restrictions, service availability, tenant policy.

Eligible offers ранжируются по стратегии.

Optimal использует внутреннюю конфигурацию price/time/SLA/risk; веса скрыты от оператора.

Recommendation хранит explanation facts, а не маркетинговый текст, чтобы UI мог локализовать объяснение.

Если confidence низкий, UI получает это явно; система не должна изображать точность без данных.

Auto Select разрешен только при выполнении всех hard constraints и tenant auto-select policy.

## 10. Tracking и exception management

Webhook предпочтителен; polling — fallback.

События append-only, dedup по carrier event ID/hash.

Отсутствие обновлений сверх порога может создавать Exception/Delayed signal.

Delivered закрывает SLA outcome, но actual cost может появиться позже из invoice.

Повторная доставка/возврат должны поддерживаться через event metadata без разрушения основной timeline.

## 11. Carrier Intelligence v1

Считать по lane/service и rolling period: shipments, on-time %, median/percentile transit, exception rate, claims/damage rate, cost variance.

Carrier Score 0–100 — производный показатель; хранить компоненты и version formula.

Confidence зависит от размера/свежести выборки.

При недостатке собственных данных recommendation может использовать заявленный ETA и нейтральный reliability prior; UI показывает low confidence.

## 12. Интеграции клиента

REST API — основной внешний интерфейс.

Webhooks Logistics OS → client system для shipment/tracking/exception updates.

CSV/XLSX import допустим как onboarding fallback, но не является основной архитектурой.

1С интеграция — отдельный коннектор поверх public Logistics OS API, а не отдельная бизнес-логика.

## 13. Эксплуатационные требования

Dev / Stage / Prod раздельно.

Feature flags для adapters и Auto Select.

Secrets manager/encrypted storage.

Structured logs, metrics, traces, carrier health.

Daily backups + проверяемый restore procedure.

Audit log для изменения правил, credentials, insurance override, manual decision, cancellation.

## 14. План запуска

| Этап | Содержание | Exit criteria |

|---|---|---|

| 0. Foundation | Core schema, auth, OpenAPI, mocks. | FE/BE могут работать параллельно. |

| 1. 2 carriers | Major Express + один REST carrier. | Quote/Create/Track E2E. |

| 2. 5 carriers | CDEK, ДЛ, ПЭК, Яндекс. | Partial success + normalization. |

| 3. Pilot | 3–5 клиентов, manual recommendation. | 10k shipments, quality metrics. |

| 4. Auto Select | Rules + safe automation. | Controlled cohort, rollback. |

| 5. Intelligence | Carrier Score calibration. | Достаточная выборка по ключевым lanes. |

## Источники по API перевозчиков

Источники используются для фиксации возможностей интеграции. Конкретные поля и методы должны быть повторно проверены разработчиком при подключении боевых учетных данных, так как API перевозчиков могут изменяться.

| Источник | URL | Что подтверждает |

|---|---|---|

| Major Express developer wiki | https://developers.major-express.ru/web-servisy-2/ | Клиентские веб-сервисы для экспресс-доставки и сборных грузов; SOAP/WSDL, методы расчета, создания накладной и справочники. |

| Major Express Calculator1 | https://developers.major-express.ru/web-servisy-2/web-servis-klientskij/ekspress-dostavka/metody/prochie/kalkulyator-stoimostej-i-srokov-s-uchetom-skidok-klienta-s-ukazaniem-parametrov-mest/ | Расчет стоимости и срока с учетом договора клиента и параметров мест. |

| CDEK Developers | https://developer.cdek.ru/ | Официальная платформа интеграции СДЭК; расчет, накладные, статусы, документы и интеграционные сценарии. |

| Деловые Линии API | https://dev.dellin.ru/api/ | Официальная документация API. |

| Деловые Линии Calculator | https://dev.dellin.ru/api/examples/calculation/ | Расчет стоимости/сроков через API. |

| ПЭК Public API | https://pecom.ru/business/developers/api_public/ | Публичный расчет стоимости и дополнительных услуг; расширенные функции через API личного кабинета. |

| ПЭК Calculator API | https://test-kabinet.pecom.ru/preweb/api/v1/help/calculator | Расчет стоимости и сроков для зарегистрированных пользователей. |

| Яндекс Доставка API | https://yandex.com/support/delivery-profile/ru/api/ | HTTP API управления доставкой. |

| Яндекс Доставка quickstart | https://yandex.com/support/delivery-profile/ru/api/express/quickstart | Расчет, создание, подтверждение и отслеживание заказа. |

| Яндекс Доставка по России | https://yandex.com/support/delivery-profile/ru/api/other-day/ | API доставки по России, тестовый и боевой контуры. |
