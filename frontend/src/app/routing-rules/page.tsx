'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import {
  request,
  tokens,
  type CarrierConnection,
  type City,
  type RoutingRule,
  type RoutingRules,
} from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { CityPicker } from '@/components/CityPicker'
import { ErrorNote } from '@/components/ErrorNote'
import { formatDateTime } from '@/lib/format'
import {
  ACTION_LABELS,
  CARGO_TYPE_LABELS,
  SELECTION_LABELS,
  citiesInRules,
  describeAction,
  describeConditions,
  type RuleActionKind,
} from '@/lib/routingRules'
import styles from './page.module.css'
import buttons from '@/styles/buttons.module.css'

/** Действия, которые вправе смотреть на перевозчика (ADR-0028, §4.2). */
const CARRIER_AWARE: RuleActionKind[] = ['deny', 'allow']

/** Опасность — вторая ось, а не значение типа груза, поэтому три состояния. */
const DANGEROUS_OPTIONS = [
  { value: '', label: 'не важно' },
  { value: 'true', label: 'только опасные' },
  { value: 'false', label: 'только неопасные' },
]

interface Draft {
  name: string
  priority: string
  action: RuleActionKind
  selection: string
  carriers: string[]
  cargoTypes: string[]
  dangerous: string
  from: string[]
  to: string[]
  minKg: string
  maxKg: string
  minValue: string
  currency: string
}

const EMPTY: Draft = {
  name: '',
  priority: '',
  action: 'deny',
  selection: 'cheapest',
  carriers: [],
  cargoTypes: [],
  dangerous: '',
  from: [],
  to: [],
  minKg: '',
  maxKg: '',
  minValue: '',
  currency: 'RUB',
}

/** Черновик формы → тело запроса. Пустое поле не превращается в условие. */
function toBody(draft: Draft) {
  const conditions: Record<string, unknown> = {}
  if (CARRIER_AWARE.includes(draft.action) && draft.carriers.length > 0) {
    conditions.carrier = draft.carriers
  }
  if (draft.cargoTypes.length > 0) conditions.cargo_type = draft.cargoTypes
  if (draft.dangerous) conditions.dangerous = draft.dangerous === 'true'

  // Направление без единой стороны — это не «любое направление», а условие,
  // которое бэкенд отвергнет: оно не значит ничего.
  if (draft.from.length > 0 || draft.to.length > 0) {
    conditions.direction = {
      ...(draft.from.length > 0 && { from: draft.from }),
      ...(draft.to.length > 0 && { to: draft.to }),
    }
  }

  const min = kgToGrams(draft.minKg)
  const max = kgToGrams(draft.maxKg)
  if (min !== null || max !== null) {
    conditions.weight = {
      ...(min !== null && { min_grams: min }),
      ...(max !== null && { max_grams: max }),
    }
  }

  const value = majorToMinor(draft.minValue)
  if (value !== null) conditions.cargo_value = { min_minor: value, currency: draft.currency }

  const actions: Record<string, unknown> =
    draft.action === 'auto_select' ? { auto_select: draft.selection } : { [draft.action]: true }

  return {
    name: draft.name.trim(),
    priority: Number(draft.priority),
    conditions,
    actions,
  }
}

function kgToGrams(value: string): number | null {
  const parsed = Number(value.replace(',', '.'))
  return value.trim() && Number.isFinite(parsed) ? Math.round(parsed * 1000) : null
}

function majorToMinor(value: string): number | null {
  const parsed = Number(value.replace(',', '.'))
  return value.trim() && Number.isFinite(parsed) ? Math.round(parsed * 100) : null
}

export default function RoutingRulesPage() {
  const router = useRouter()
  const queryClient = useQueryClient()

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [open, setOpen] = useState(false)

  const rules = useQuery({
    queryKey: ['routing-rules'],
    queryFn: () => request<RoutingRules>('/routing-rules'),
  })

  const carriers = useQuery({
    queryKey: ['carrier-connections'],
    queryFn: () => request<CarrierConnection[]>('/carriers'),
  })

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['routing-rules'] })
  }

  const create = useMutation({
    mutationFn: () =>
      request<RoutingRule>('/routing-rules', { method: 'POST', body: toBody(draft) }),
    onSuccess: () => {
      setDraft(EMPTY)
      setOpen(false)
      refresh()
    },
  })

  const toggle = useMutation({
    mutationFn: (rule: RoutingRule) =>
      request<RoutingRule>(`/routing-rules/${rule.id}`, {
        method: 'PATCH',
        body: { enabled: !rule.enabled },
      }),
    onSuccess: refresh,
  })

  const remove = useMutation({
    mutationFn: (rule: RoutingRule) =>
      request<void>(`/routing-rules/${rule.id}`, { method: 'DELETE' }),
    onSuccess: refresh,
  })

  const items = rules.data?.items ?? []
  const connected = (carriers.data ?? []).filter((carrier) => carrier.connected)

  // Названия городов для условий по направлению: в правиле лежат только
  // идентификаторы ФИАС, и без этого запроса направление читалось бы как
  // «2 города» — проверить такое правило нельзя, исправить тем более.
  const usedCities = citiesInRules(items)
  const cities = useQuery({
    queryKey: ['cities', usedCities],
    queryFn: () =>
      request<City[]>(`/cities?${usedCities.map((id) => `fias_id=${id}`).join('&')}`),
    enabled: usedCities.length > 0,
  })
  const cityNames = new Map((cities.data ?? []).map((city) => [city.fias_id, city.name]))

  return (
    <AppShell>
      <h1>Правила маршрутизации</h1>
      <p className={styles.hint}>
        Корпоративная политика: кого можно спрашивать, что обязательно страховать и когда выбор
        можно сделать без человека. Правила применяются <b>до опроса перевозчиков</b>:
        запрещённого не спрашивают вовсе — вызов стоит денег и времени, — но из выдачи он не
        исчезает: строка остаётся с причиной и без цены.
      </p>

      <div className={styles.toolbar}>
        <div className={styles.policy}>
          <span className={styles.muted}>Версия политики</span>
          <code className={styles.version}>{rules.data?.policy_version ?? '—'}</code>
          <span className={styles.muted}>
            попадает в снимок каждого решения: по ней видно, по каким правилам оно принято
          </span>
        </div>
        <button
          type="button"
          className={buttons.primary}
          onClick={() => setOpen((was) => !was)}
        >
          {open ? 'Отмена' : 'Новое правило'}
        </button>
      </div>

      {rules.isError && <ErrorNote error={rules.error} />}

      {open && (
        <form
          className={styles.form}
          onSubmit={(event) => {
            event.preventDefault()
            create.mutate()
          }}
        >
          <div className={styles.row}>
            <label className={styles.field}>
              Название
              <input
                value={draft.name}
                required
                maxLength={255}
                placeholder="Например: опасные грузы — не Почтой"
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              />
            </label>
            <label className={styles.narrow}>
              Приоритет
              <input
                type="number"
                min={0}
                required
                value={draft.priority}
                onChange={(e) => setDraft({ ...draft, priority: e.target.value })}
              />
              <span className={styles.muted}>больше — сильнее</span>
            </label>
          </div>

          <div className={styles.row}>
            <label className={styles.field}>
              Действие
              <select
                value={draft.action}
                onChange={(e) =>
                  setDraft({ ...draft, action: e.target.value as RuleActionKind })
                }
              >
                {(Object.keys(ACTION_LABELS) as RuleActionKind[]).map((kind) => (
                  <option key={kind} value={kind}>
                    {ACTION_LABELS[kind]}
                  </option>
                ))}
              </select>
            </label>
            {draft.action === 'auto_select' && (
              <label className={styles.field}>
                Выбирать
                <select
                  value={draft.selection}
                  onChange={(e) => setDraft({ ...draft, selection: e.target.value })}
                >
                  {Object.entries(SELECTION_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>

          {CARRIER_AWARE.includes(draft.action) ? (
            <fieldset className={styles.group}>
              <legend>Перевозчики</legend>
              <p className={styles.muted}>
                Пусто — правило про всех. Для whitelist пустой список означал бы «разрешено
                всё», то есть правило без действия.
              </p>
              <div className={styles.checks}>
                {connected.map((carrier) => (
                  <label key={carrier.code} className={styles.check}>
                    <input
                      type="checkbox"
                      checked={draft.carriers.includes(carrier.code)}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          carriers: e.target.checked
                            ? [...draft.carriers, carrier.code]
                            : draft.carriers.filter((code) => code !== carrier.code),
                        })
                      }
                    />
                    {carrier.name}
                  </label>
                ))}
                {connected.length === 0 && (
                  <span className={styles.muted}>Ни один перевозчик ещё не подключён.</span>
                )}
              </div>
            </fieldset>
          ) : (
            <p className={styles.notice}>
              Страхование и автовыбор — решения по всей отправке: страхование входит в цену у
              всех, кого спрашивают, а автовыбор берёт лучший вариант из выдачи целиком. Условие
              по перевозчику рядом с ними выглядело бы работающим и не значило бы ничего,
              поэтому его здесь нет.
            </p>
          )}

          <fieldset className={styles.group}>
            <legend>Условия</legend>
            <div className={styles.row}>
              <label className={styles.narrow}>
                Вес от, кг
                <input
                  inputMode="decimal"
                  value={draft.minKg}
                  onChange={(e) => setDraft({ ...draft, minKg: e.target.value })}
                />
              </label>
              <label className={styles.narrow}>
                Вес до, кг
                <input
                  inputMode="decimal"
                  value={draft.maxKg}
                  onChange={(e) => setDraft({ ...draft, maxKg: e.target.value })}
                />
              </label>
              <label className={styles.narrow}>
                Стоимость от
                <input
                  inputMode="decimal"
                  value={draft.minValue}
                  onChange={(e) => setDraft({ ...draft, minValue: e.target.value })}
                />
              </label>
              <label className={styles.narrow}>
                Валюта
                <input
                  value={draft.currency}
                  maxLength={3}
                  onChange={(e) =>
                    setDraft({ ...draft, currency: e.target.value.toUpperCase() })
                  }
                />
              </label>
              <label className={styles.narrow}>
                Опасность
                <select
                  value={draft.dangerous}
                  onChange={(e) => setDraft({ ...draft, dangerous: e.target.value })}
                >
                  {DANGEROUS_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <p className={styles.muted}>
              Вес — <b>расчётный</b>, с учётом объёмного: правило «тяжелее 30 кг» обязано
              срабатывать на том же числе, которое попадёт в счёт.
            </p>

            <div className={styles.row}>
              <CityPicker
                label="Откуда"
                value={draft.from}
                onChange={(from) => setDraft({ ...draft, from })}
              />
              <CityPicker
                label="Куда"
                value={draft.to}
                onChange={(to) => setDraft({ ...draft, to })}
              />
            </div>
            <p className={styles.muted}>
              Пустая сторона означает «любая»: «Куда — Калининград» без «Откуда» — это правило
              про всё, что едет в Калининград. Регионов в условии нет намеренно: регион в адресе
              необязателен и в снимках обычно пуст, а правило, собранное из пустого поля, молча
              не совпало бы ни с чем.
            </p>
            <div className={styles.checks}>
              {Object.entries(CARGO_TYPE_LABELS).map(([value, label]) => (
                <label key={value} className={styles.check}>
                  <input
                    type="checkbox"
                    checked={draft.cargoTypes.includes(value)}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        cargoTypes: e.target.checked
                          ? [...draft.cargoTypes, value]
                          : draft.cargoTypes.filter((type) => type !== value),
                      })
                    }
                  />
                  {label}
                </label>
              ))}
            </div>
          </fieldset>

          {create.isError && <ErrorNote error={create.error} />}

          <button type="submit" className={buttons.primary} disabled={create.isPending}>
            {create.isPending ? 'Сохраняем…' : 'Создать правило'}
          </button>
        </form>
      )}

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Приоритет</th>
              <th>Название</th>
              <th>Условия</th>
              <th>Действие</th>
              <th>Изменено</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((rule) => (
              <tr key={rule.id} className={rule.enabled ? undefined : styles.off}>
                <td>{rule.priority}</td>
                <td className={styles.strong}>{rule.name}</td>
                <td>
                  <ul className={styles.conditions}>
                    {describeConditions(rule.conditions, cityNames).map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                </td>
                <td>{describeAction(rule.actions)}</td>
                <td className={styles.muted}>{formatDateTime(rule.updated_at)}</td>
                <td className={styles.actions}>
                  <button type="button" onClick={() => toggle.mutate(rule)}>
                    {rule.enabled ? 'Выключить' : 'Включить'}
                  </button>
                  <button
                    type="button"
                    className={buttons.danger}
                    onClick={() => remove.mutate(rule)}
                  >
                    Удалить
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {!rules.isLoading && items.length === 0 && (
          <p className={styles.empty}>
            Правил нет: расчёт спрашивает всех подключённых перевозчиков, страхование остаётся
            на усмотрение оператора, решение принимает человек.
          </p>
        )}
      </div>

      {(toggle.isError || remove.isError) && <ErrorNote error={toggle.error ?? remove.error} />}

      <p className={styles.hint}>
        Уже принятые решения правки правил не меняют: каждое несёт версию той политики, по
        которой принято. Пересчёт истории по новым правилам сделал бы аналитику несопоставимой.
      </p>
    </AppShell>
  )
}
