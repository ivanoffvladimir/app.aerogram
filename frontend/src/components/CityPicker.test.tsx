import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CityPicker } from './CityPicker'

const request = vi.hoisted(() => vi.fn())

vi.mock('@/api/client', () => ({ request }))

const MOSCOW = '0c5b2444-70a0-4932-980c-b4dc0d3f02b5'
const VLADIVOSTOK = '7b6de6a5-86d0-4735-b11a-499081111af8'

/** Ответы подсказок и справочника различаются по пути, а не по порядку:
 *  запросы идут параллельно, и опора на порядок дала бы тест, который
 *  зеленеет через раз. */
function answer(paths: Record<string, unknown>) {
  request.mockImplementation((path: string) => {
    for (const [prefix, value] of Object.entries(paths)) {
      if (path.startsWith(prefix)) return Promise.resolve(value)
    }
    return Promise.reject(new Error(`неожиданный путь: ${path}`))
  })
}

function wrapper({ children }: { children: ReactNode }) {
  // Повторы выключены: тест не должен ждать бэкоффа, а сбой запроса
  // обязан быть виден сразу.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

beforeEach(() => {
  request.mockReset()
})

describe('CityPicker', () => {
  it('отдаёт наружу идентификатор ФИАС, а не название', async () => {
    // Одинаковых названий в России достаточно, чтобы «Ростов» без
    // идентификатора означал два разных города.
    const user = userEvent.setup()
    answer({
      '/cities/suggest': {
        items: [{ fias_id: MOSCOW, name: 'Москва', full_name: 'г Москва', region: null }],
        degraded: false,
        degraded_reason: null,
      },
    })
    const onChange = vi.fn()

    render(<CityPicker label="Куда" value={[]} onChange={onChange} />, { wrapper })
    await user.type(screen.getByPlaceholderText('Начните вводить город'), 'Моск')

    await user.click(await screen.findByRole('button', { name: /Москва/ }))
    expect(onChange).toHaveBeenCalledWith([MOSCOW])
  })

  it('не спрашивает подсказки на один символ', async () => {
    // Подсказки у ДаData квотируются, и запрос на каждую букву тратит
    // квоту на слово, которое ещё не дописали.
    const user = userEvent.setup()
    answer({ '/cities/suggest': { items: [], degraded: false, degraded_reason: null } })

    render(<CityPicker label="Куда" value={[]} onChange={vi.fn()} />, { wrapper })
    await user.type(screen.getByPlaceholderText('Начните вводить город'), 'М')

    // Ожидание обёрнуто в act: пауза ввода действительно меняет состояние
    // компонента, и без обёртки React справедливо ругается, что обновление
    // прошло мимо теста.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400))
    })
    expect(request).not.toHaveBeenCalled()
  })

  it('выбранные города показывает названиями', async () => {
    // В условии правила лежат только идентификаторы: показывать их
    // человеку бессмысленно.
    answer({
      '/cities?': [{ id: 'x', fias_id: MOSCOW, name: 'Москва', full_name: null, region: null }],
    })

    render(<CityPicker label="Куда" value={[MOSCOW]} onChange={vi.fn()} />, { wrapper })

    expect(await screen.findByText('Москва')).toBeInTheDocument()
  })

  it('пока названия не пришли, показывает идентификатор, а не пустоту', () => {
    // Строка есть, город выбран — пустая ячейка выглядела бы потерей выбора.
    answer({ '/cities?': [] })
    render(<CityPicker label="Куда" value={[VLADIVOSTOK]} onChange={vi.fn()} />, { wrapper })
    expect(screen.getByText(VLADIVOSTOK)).toBeInTheDocument()
  })

  it('уже выбранный город не предлагается второй раз', async () => {
    const user = userEvent.setup()
    answer({
      '/cities/suggest': {
        items: [{ fias_id: MOSCOW, name: 'Москва', full_name: 'г Москва', region: null }],
        degraded: false,
        degraded_reason: null,
      },
      '/cities?': [{ id: 'x', fias_id: MOSCOW, name: 'Москва', full_name: null, region: null }],
    })

    render(<CityPicker label="Куда" value={[MOSCOW]} onChange={vi.fn()} />, { wrapper })
    await user.type(screen.getByPlaceholderText('Начните вводить город'), 'Моск')

    await waitFor(() =>
      expect(request).toHaveBeenCalledWith(expect.stringContaining('suggest')),
    )
    // Название есть в списке выбранных, но кнопки выбора для него быть не должно.
    expect(screen.queryByRole('button', { name: /^Москва/ })).not.toBeInTheDocument()
  })

  it('говорит, когда подсказки собраны без ДаData', async () => {
    // Иначе оператор решит, что города не существует, хотя его просто
    // нет в нашем локальном справочнике.
    const user = userEvent.setup()
    answer({
      '/cities/suggest': {
        items: [],
        degraded: true,
        degraded_reason: 'Справочник адресов не настроен',
      },
    })

    render(<CityPicker label="Куда" value={[]} onChange={vi.fn()} />, { wrapper })
    await user.type(screen.getByPlaceholderText('Начните вводить город'), 'Моск')

    expect(await screen.findByText(/Справочник адресов не настроен/)).toBeInTheDocument()
  })
})
