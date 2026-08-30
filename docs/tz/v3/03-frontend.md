# Logistics OS — Финальное ТЗ для Front-end разработчика

Implementation Specification • версия 3.0

ФИНАЛЬНАЯ РЕДАКЦИЯ ДЛЯ СТАРТА РАЗРАБОТКИ

## 1. Технологический baseline

Next.js + TypeScript strict.

React Query/TanStack Query для server state.

React Hook Form + schema validation.

OpenAPI-generated/typed API client.

Playwright E2E; Vitest/Jest + Testing Library.

Dockerized build; secrets не попадают в browser bundle.

## 2. Информационная архитектура

| Route | Экран | P |

|---|---|---|

| /login | Login | P0 |

| /dashboard | Dashboard | P1 |

| /shipments | Shipments registry | P0 |

| /shipments/new | Create shipment | P0 |

| /rate-shopping | Rate Shopping | P0 |

| /shipments/{id} | Shipment Details | P0 |

| /tracking | Tracking Center | P0 |

| /carriers | Carrier Accounts | P1 |

| /carrier-score | Carrier Score | P1 |

| /routing-rules | Routing Rules | P1 |

| /invoices | Costs/Invoices | P2 |

| /integrations | Integrations | P1 |

| /settings/users | Users/RBAC | P1 |

## 3. Общие UI states

loading/skeleton, empty, success, partial success, stale, validation error, forbidden, not found, upstream error.

Ни один carrier timeout не должен превращать Rate Shopping в общий error, если есть другие offers.

request_id показывается в technical error details для поддержки.

Форма shipment draft сохраняет введенные данные при transient error.

## 4. Rate Shopping — экран P0

Header: маршрут, места/вес, cargo value, deadline, insurance policy.

Strategy tabs: Оптимальный / Самый дешевый / Самый быстрый / Самый надежный.

Recommended hero card: carrier/service/source, Total Cost, ETA, deadline margin, on-time %, label, Carrier Score, Risk, Confidence, insurance, explanation.

Cost breakdown drawer: base + insurance + surcharges.

Client tariff и Logistics OS tariff визуально маркируются.

Eligible alternatives ниже; late/ineligible — отдельным приглушенным блоком с причиной.

No deadline match — красное предупреждение + ближайшие альтернативы.

Partial carrier failure — компактный warning, не блокирующий выбор.

Stale quote — disable Select/Create и CTA «Пересчитать».

## 5. Recommendation / Override

Frontend не рассчитывает recommendation самостоятельно.

При смене strategy вызывает routing endpoint либо использует backend-provided precomputed result; источник истины — backend.

Если selected_offer != recommended_offer, открыть Override dialog.

Reasons: cheaper, faster, recipient requirement, corporate contract/policy, negative experience, carrier preference, other.

Обязательность reason определяется tenant policy из backend.

После POST decision сохранить decision_id/snapshot_id и только затем создавать shipment.

## 6. Auto Select UX

Auto Select не должен выглядеть как скрытая автоматизация: UI показывает, что правило активно.

Перед автоматическим созданием backend подтверждает eligibility/policy.

Состояния: auto-selected, manual review required, blocked by deadline/policy, failed upstream.

В Shipment Details показывать «Selected automatically» + rule/policy version.

## 7. Shipment Details

Summary: carrier, service, tracking, status, deadline, ETA, Total Cost.

Decision block: recommended vs selected, strategy, override, explanation snapshot.

Tracking timeline: normalized status + carrier status + time/location.

Documents: label/waybill where available.

Actual outcome after delivery: delivered time, deadline hit/miss, actual cost, damage/claim.

## 8. Carrier Accounts

Список подключенных договоров, а не только брендов перевозчиков.

Показывать source/client contract, masked account identifier, capabilities, health, last successful call.

Credentials никогда не возвращаются/не отображаются после сохранения.

Test connection action вызывает backend health check.

## 9. RBAC UI

| Permission | Пример |

|---|---|

| shipment.create | Создание отправления. |

| shipment.cancel | Отмена. |

| insurance.override | Изменение обязательного страхования. |

| routing.auto_select | Включение автоматического выбора. |

| routing.rules.manage | Правила. |

| billing.view | Финансы. |

| integrations.manage | Credentials/API. |

| users.manage | Пользователи/роли. |

## 10. API contract rules

Использовать финальный OpenAPI из пакета как contract source.

Money отображается из amount_minor/currency; float arithmetic для денег запрещена.

Все timestamps timezone-aware; отображать в timezone tenant/user.

Не зависеть от raw carrier payload.

Unknown enum/status должен иметь safe fallback, а не ломать экран.

## 11. Analytics events

rate_requested, rate_partial_failure, strategy_changed, recommendation_viewed, offer_selected, override_opened, override_confirmed, shipment_created, tracking_exception_opened.

Не отправлять credentials, телефоны/адреса и чувствительные shipment payload в продуктовую аналитику.

## 12. E2E acceptance suite P0

| Scenario | Expected |

|---|---|

| Happy path | Login → draft → rates → recommendation → decision → shipment → tracking. |

| Partial success | 1 carrier timeout; остальные offers доступны. |

| Deadline miss | Late offers visible/inactive; nearest alternatives shown. |

| Mandatory insurance | Insurance included; unauthorized override denied. |

| Override | Reason saved and visible in decision history. |

| Stale quote | Create disabled until refresh. |

| Duplicate click | Idempotent backend prevents duplicate shipment; UI handles response. |

| RBAC | Viewer cannot mutate. |

| Auto Select blocked | No eligible offer → manual review. |

## 13. Definition of Done Frontend

Все P0 screens и states реализованы.

OpenAPI types синхронизированы.

Accessibility: keyboard navigation, labels, focus states, contrast.

Responsive desktop/tablet; сложные настройки могут быть desktop-only в MVP.

No console errors; TypeScript strict; lint/test CI green.

Playwright P0 suite green на Stage.

Observability/error reporting подключены.

Security review: no secrets in localStorage/bundle/logs.
