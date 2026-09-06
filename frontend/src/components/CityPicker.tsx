'use client'

import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { request, type City, type CitySuggestResponse } from '@/api/client'
import { MAX_CITY_LOOKUP } from '@/lib/routingRules'
import styles from './CityPicker.module.css'

/**
 * Выбор нескольких городов подсказками ФИАС.
 *
 * Города здесь называются идентификатором ФИАС, а не названием: одинаковых
 * названий в России достаточно, чтобы «Ростов» без идентификатора означал
 * два разных города в тысяче километров друг от друга. Наружу компонент
 * отдаёт именно идентификаторы — в том виде, в каком их ждёт условие правила
 * маршрутизации.
 *
 * Уже выбранные показываются названиями: правило, направление которого
 * читается только как «3 города», нельзя ни проверить, ни исправить.
 */

/** Со скольких символов есть смысл спрашивать подсказки. */
const MIN_QUERY = 2

/** Пауза после ввода. Подсказки у ДаData квотируются, и запрос на каждую
 *  букву тратит квоту на слова, которых пользователь ещё не дописал. */
const DEBOUNCE_MS = 300

interface Props {
  label: string
  /** Идентификаторы ФИАС выбранных городов. */
  value: string[]
  onChange: (fiasIds: string[]) => void
}

export function CityPicker({ label, value, onChange }: Props) {
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query])

  const suggestions = useQuery({
    queryKey: ['city-suggest', debounced],
    queryFn: () =>
      request<CitySuggestResponse>(`/cities/suggest?q=${encodeURIComponent(debounced)}`),
    enabled: debounced.length >= MIN_QUERY,
  })

  // Названия выбранных городов приходят отдельным запросом: в условии
  // правила лежат только идентификаторы, а показывать их человеку
  // бессмысленно.
  // Список подрезан пределом пути: сверх него названия не придут,
  // и лишние города останутся идентификаторами. Отправить длинный список
  // целиком значило бы получить 422 и не показать НИ ОДНОГО названия.
  const lookup = value.slice(0, MAX_CITY_LOOKUP)
  const chosen = useQuery({
    queryKey: ['cities', [...lookup].sort()],
    queryFn: () => request<City[]>(`/cities?${lookup.map((id) => `fias_id=${id}`).join('&')}`),
    enabled: lookup.length > 0,
  })

  const names = new Map((chosen.data ?? []).map((city) => [city.fias_id, city.name]))
  const items = (suggestions.data?.items ?? []).filter((item) => !value.includes(item.fias_id))

  return (
    <div className={styles.picker}>
      <span className={styles.label}>{label}</span>

      {value.length > 0 && (
        <ul className={styles.chosen}>
          {value.map((fiasId) => (
            <li key={fiasId} className={styles.chip}>
              {/* Пока названия не пришли — идентификатор. Он бесполезен
                  человеку, но честнее пустоты: строка есть, город выбран. */}
              {names.get(fiasId) ?? fiasId}
              <button
                type="button"
                aria-label={`Убрать ${names.get(fiasId) ?? fiasId}`}
                onClick={() => onChange(value.filter((id) => id !== fiasId))}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      <input
        value={query}
        placeholder="Начните вводить город"
        onChange={(event) => setQuery(event.target.value)}
      />

      {suggestions.data?.degraded && (
        <p className={styles.degraded}>
          Подсказки собраны из нашего справочника:{' '}
          {suggestions.data.degraded_reason ?? 'внешний сервис недоступен'}. Города, которых в
          нём ещё нет, здесь не появятся.
        </p>
      )}

      {items.length > 0 && (
        <ul className={styles.options}>
          {items.map((item) => (
            <li key={item.fias_id}>
              <button
                type="button"
                onClick={() => {
                  onChange([...value, item.fias_id])
                  setQuery('')
                  setDebounced('')
                }}
              >
                <span className={styles.name}>{item.name}</span>
                <span className={styles.region}>{item.region ?? item.full_name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {debounced.length >= MIN_QUERY && !suggestions.isLoading && items.length === 0 && (
        <p className={styles.empty}>Ничего не найдено.</p>
      )}
    </div>
  )
}
