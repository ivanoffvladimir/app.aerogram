import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import { detailsToText, errorMessage, technicalDetails } from '@/lib/errorDetails'

function apiError(overrides: Partial<Record<string, unknown>> = {}): ApiError {
  return new ApiError(422, {
    error: {
      code: 'validation_failed',
      message: 'Данные не прошли проверку',
      field: 'city',
      carrier_code: null,
      request_id: 'IIuhgQW2Qhn5AR7F',
      ...overrides,
    },
  })
}

describe('errorMessage', () => {
  it('сообщение бэкенда показывается как есть', () => {
    // Единый формат ошибки уже отдаёт русский текст.
    expect(errorMessage(apiError())).toBe('Данные не прошли проверку')
  })

  it('обрыв связи не показывается по-английски', () => {
    // fetch роняет TypeError, и «Failed to fetch» посреди русского кабинета
    // выглядит поломкой кабинета, а не сети.
    const message = errorMessage(new TypeError('Failed to fetch'))
    expect(message).toContain('связаться с сервером')
    expect(message).not.toContain('fetch')
  })

  it('незнакомая ошибка не показывает своё внутреннее сообщение', () => {
    expect(errorMessage(new Error('TypeError: undefined is not a function'))).toBe(
      'Не удалось выполнить запрос',
    )
  })

  it('не-ошибка тоже даёт текст, а не пустоту', () => {
    expect(errorMessage(undefined)).toBe('Не удалось выполнить запрос')
    expect(errorMessage({})).toBe('Не удалось выполнить запрос')
  })

  it('наше собственное сообщение проверки формы показывается как есть', () => {
    // Строка сюда попадает только от нас: браузер бросает объекты.
    // Подменить её общим текстом значило бы спрятать единственную
    // подсказку, которая человеку и нужна.
    expect(errorMessage('Введите шесть цифр из приложения')).toBe(
      'Введите шесть цифр из приложения',
    )
  })
})

describe('technicalDetails', () => {
  it('идентификатор запроса идёт первым', () => {
    // По нему поддержка находит запись в логах — остальное сужает поиск.
    const details = technicalDetails(apiError())
    expect(details[0]).toEqual({
      label: 'Идентификатор запроса',
      value: 'IIuhgQW2Qhn5AR7F',
    })
  })

  it('без идентификатора остаются код и статус', () => {
    // Запрос мог не дойти до сервера: подробности всё равно сужают поиск.
    const details = technicalDetails(apiError({ request_id: null }))
    expect(details.map((d) => d.label)).toEqual(['Код ошибки', 'Код HTTP'])
  })

  it('поле формы в технические детали не попадает', () => {
    // Оно уже видно рядом с полем и подсказывает, где искать, а не что
    // сообщать поддержке.
    const values = technicalDetails(apiError()).map((d) => d.value)
    expect(values).not.toContain('city')
  })

  it('у ошибки не от бэкенда подробностей нет', () => {
    expect(technicalDetails(new TypeError('Failed to fetch'))).toEqual([])
  })
})

describe('detailsToText', () => {
  it('копируется целиком, а не по одному значению', () => {
    // Человек, читающий идентификатор с экрана вслух, ошибётся в нём.
    expect(detailsToText(apiError())).toBe(
      'Идентификатор запроса: IIuhgQW2Qhn5AR7F\nКод ошибки: validation_failed\nКод HTTP: 422',
    )
  })

  it('нечего копировать — пустая строка, а не «undefined»', () => {
    expect(detailsToText(new TypeError('нет сети'))).toBe('')
  })
})
