# Logistics OS — Финальное ТЗ для Back-end разработчика

Architecture, API, Data, Carrier Adapters • версия 3.0

ФИНАЛЬНАЯ РЕДАКЦИЯ ДЛЯ СТАРТА РАЗРАБОТКИ

## 1. Архитектурное решение MVP

Modular monolith: Auth/Tenant, Carrier, Rate, Routing, Shipment, Tracking, Intelligence, Billing, Audit.

FastAPI/Python baseline; PostgreSQL; Redis; background workers; S3-compatible object storage.

Carrier adapters — отдельные модули с единым интерфейсом.

OpenAPI 3.1 — контракт с Frontend и внешними клиентами.

Переход к отдельным сервисам допускается только по измеренной нагрузке/организационной необходимости.

## 2. Multi-tenancy и безопасность

tenant_id обязателен для tenant-owned сущностей; проверка tenant scope в service/repository layer и тестах.

RBAC backend-side; frontend не является security boundary.

Carrier credentials шифруются at rest и никогда не попадают в response/log/snapshot.

JWT/session strategy с refresh; rotation/revocation policy.

PII минимизировать в логах; audit отдельно от operational logs.

Rate limiting для public API; webhook verification там, где carrier поддерживает подпись/secret.

## 3. Data model — обязательные сущности

| Entity | Ключевые поля/правила |

|---|---|

| Tenant | id, name, timezone, policies. |

| User/Role/Permission | tenant scoped RBAC. |

| Carrier | global code/name. |

| CarrierAccount | tenant, carrier, source, encrypted credentials, capabilities. |

| CarrierService | normalized + carrier code. |

| RateQuote | input_snapshot, strategy, valid_until. |

| RateOffer | quote, account, service, source, ETA, eligible, Total Cost. |

| CostComponent | type, amount_minor, currency, rate_percent. |

| Recommendation | quote, offer, strategy, explanation_facts, algorithm_version, policy_version. |

| Decision | recommendation, selected_offer, actor, override, reason, mode. |

| Shipment | decision, carrier shipment id, tracking, status, deadline, ETA. |

| TrackingEvent | append-only normalized + original. |

| DeliveryOutcome | delivered_at, deadline_met, actual cost, damage, claim. |

| CarrierScore | scope, metrics, score, risk, confidence, formula_version. |

| RoutingRule | priority, conditions, actions, enabled. |

| AuditLog | actor, action, entity, before/after refs, timestamp. |

## 4. Database rules

UUID public IDs.

Money = BIGINT amount_minor + CHAR(3) currency.

timestamptz UTC internally.

FK/UNIQUE/CHECK constraints for invariants.

JSONB only for immutable snapshots, policy conditions/actions, carrier extensions; filter-critical data in columns.

Decision/Recommendation/Offer snapshots immutable after decision.

Indexes: tenant+created_at, shipment status, tracking number, quote valid_until, events shipment+occurred_at, carrier score scope.

## 5. API workflow

| Endpoint | Rule |

|---|---|

| POST /v1/rates | Creates quote snapshot; parallel adapters; returns offers + failures + TTL. |

| POST /v1/routing/quote | Returns recommendation from quote + strategy. |

| POST /v1/decisions | Immutable decision; Idempotency-Key. |

| POST /v1/shipments | Creates carrier shipment from decision; Idempotency-Key. |

| GET /v1/shipments/{id}/tracking | Normalized timeline. |

| POST /v1/webhooks/{carrier} | Fast accept → async processing; dedup. |

| GET /v1/analytics/carriers | Aggregated score/risk/confidence. |

## 6. Idempotency

Idempotency-Key required for decision and shipment create; recommended for rate create.

Same key + same payload → same response/result.

Same key + different payload → 409.

Carrier create call must have internal operation_id and retry-safe state machine.

If carrier timeout occurs after request transmission, adapter must reconcile before retry when duplicate creation is possible.

## 7. Rate Engine algorithm

Validate shipment and tenant policy.

Resolve connected CarrierAccounts and allow/deny filters.

Fan-out async with per-carrier timeout and global deadline.

Normalize responses into RateOffer.

Add platform insurance/surcharges and compute Total Cost.

Calculate deadline_margin/lateness and eligibility.

Persist raw response reference for debugging with retention/security policy.

Return partial results immediately after global quote timeout.

## 8. Routing Engine v1

Hard constraints evaluated before scoring.

cheapest = min Total Cost among eligible.

fastest = earliest ETA among eligible.

reliable = best reliability/risk metric among eligible.

optimal = versioned score/expected-loss model combining cost, ETA margin, on-time probability and risk; no user-visible weights.

Recommendation explanation generated from structured facts: deadline fit, cost delta, SLA delta, risk delta, confidence.

Low-data fallback uses carrier ETA + neutral reliability prior and low confidence.

## 9. Carrier adapter technical contract

| Method | Input | Output |

|---|---|---|

| capabilities | account | Capability flags |

| get_rates | Normalized RateRequest | RateOffer[] |

| create_shipment | Normalized shipment + selected offer | carrier_id/tracking/docs |

| cancel_shipment | shipment | result |

| get_tracking | shipment | TrackingEvent[] |

| get_documents | shipment | document refs |

| health_check | account | health/latency |

| normalize_error | raw exception | typed upstream error |

## 10. Adapter implementation requirements

Per-carrier DTOs never leak outside adapter boundary.

Timeout/retry/circuit breaker configurable per carrier/method.

Metrics labels: carrier, method, outcome; do not label by tenant ID if cardinality becomes excessive.

Fixtures from real sanitized responses; contract tests.

Sandbox/mock support.

Adapter version included in technical metadata.

Reference-data sync jobs for cities/terminals/services where needed.

## 11. Adapter specifics for first 5 carriers

| Carrier | MVP implementation notes |

|---|---|

| Major Express | SOAP client from WSDL. Basic Auth. Implement city dictionaries, Calculator1 for contract rate, CreateWaybill for express; LTL as separate capability/sub-adapter. Preserve RequestID GUID for idempotency/correlation. |

| CDEK | Use official developer API credentials. Implement auth token lifecycle, calculator, order/waybill, tracking/status, documents. Exact endpoint schemas pinned to tested API version during Sprint 2. |

| Деловые Линии | Implement calculator /v2/calculator, location/terminal mapping, authenticated shipment/tracking methods required by pilot. Treat LTL service/additional services as structured components. |

| ПЭК | Use public calculator only for non-contract preview if desired; authenticated cabinet API required for client tariff, shipment and tracking. Separate public vs contract source explicitly. |

| Яндекс Доставка | Bearer auth. Separate flow profiles for express/same-day and Russia/other-day if endpoints/statuses differ. Quote/offer → create claim/order → accept/confirm → track. Do not force this carrier into unsupported long-haul use cases. |

## 12. Tracking state machine

Inbound webhook/poll result → dedup → raw event store/reference → normalize → append TrackingEvent → update Shipment projection → emit Logistics OS webhook.

Out-of-order events accepted; projection uses event time + transition rules.

Unknown status maps to Exception/unknown metadata, not data loss.

Delivered creates/updates DeliveryOutcome.

## 13. Carrier Intelligence aggregation

Nightly/incremental aggregates by carrier/service/lane/time window.

on_time_probability must be calibrated from observed deadline outcomes, not simple marketing SLA.

confidence considers sample size and recency.

Formula/version stored with each score.

No ML dependency for MVP; model interface should allow replacement later.

## 14. Observability / SLO

| Metric/SLO | Target/behavior |

|---|---|

| Internal API availability | Target 99.9% after pilot hardening. |

| Rate Shopping | Partial response preferred over global failure. |

| Carrier call metrics | latency, timeout, 4xx/5xx, business errors. |

| Queue | depth/age alerts. |

| Tracking lag | alert when webhook/poll processing delayed. |

| Audit | all sensitive configuration and decision overrides. |

## 15. CI/CD и environments

PR: lint, type checks, unit, DB migration check, OpenAPI validation, adapter contract tests.

Stage deploy automatically after main; Prod controlled release.

DB migrations backward-compatible where possible; rollback/forward-fix procedure.

Feature flags per carrier and tenant.

Secrets injected by environment/secret manager.

Smoke tests after deploy.

## 16. Test matrix

| Layer | Required tests |

|---|---|

| Unit | Money, insurance, deadline, scoring, status mapping. |

| DB | Constraints, tenant isolation, migrations. |

| Adapter contract | Sanitized fixtures + error mapping + timeouts. |

| Integration | Postgres/Redis/worker + carrier mocks. |

| E2E | Rate → recommendation → decision → shipment → tracking. |

| Resilience | 1–4 carriers fail; partial result remains. |

| Idempotency | Network retry does not duplicate shipment. |

| Security | RBAC, tenant breakout attempts, secret redaction. |

| Load | Pilot concurrency for rate fan-out and tracking ingestion. |

## 17. Sprint plan to first pilot

| Sprint | Deliverables |

|---|---|

| 0 / 1 week | Repo, CI, environments, auth/tenant, DB migrations, OpenAPI, mocks. |

| 1 / 2 weeks | Rate core + Major Express adapter + second REST adapter; frontend mock integration. |

| 2 / 2 weeks | Shipment create + tracking + CDEK/DL adapters. |

| 3 / 2 weeks | ПЭК/Яндекс, Rate Shopping full cost/deadline, partial failure. |

| 4 / 2 weeks | Routing strategies, Decision History, override, dashboard baseline. |

| 5 / 2 weeks | Pilot hardening, observability, RBAC, security, E2E/load, data quality. |

## 18. Backend Definition of Done для старта пилота

OpenAPI freeze для P0 endpoints.

5 adapters подключены минимум на required pilot capabilities либо явно feature-flagged, если доступ credentials задержан.

Quote/Decision/Shipment idempotency проверена.

Immutable decision snapshot реализован.

Tenant isolation security tests green.

Partial failure и stale quote обработаны.

Tracking normalization и outbound webhooks работают.

Metrics/logs/audit/backups/restore documented.

Stage E2E green; pilot runbook готов.

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
