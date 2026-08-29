/**
 * Состояние аутентификации.
 *
 * Токены живут в localStorage: кабинет и API — один origin (раздел 9.1 ТЗ),
 * поэтому переход на cookie с HttpOnly не потребует менять сетевой контур —
 * это отдельная задача с ADR.
 */
import { create } from 'zustand'
import { api, setTokenReader, type TokenPair, type UserProfile } from '@/api/client'

const ACCESS_KEY = 'aerogram.access_token'
const REFRESH_KEY = 'aerogram.refresh_token'

interface AuthState {
  accessToken: string | null
  refreshToken: string | null
  user: UserProfile | null
  isLoading: boolean
  login: (email: string, password: string, mfaCode?: string) => Promise<void>
  logout: () => void
  loadProfile: () => Promise<void>
}

function persist(tokens: TokenPair): void {
  localStorage.setItem(ACCESS_KEY, tokens.access_token)
  localStorage.setItem(REFRESH_KEY, tokens.refresh_token)
}

export const useAuthStore = create<AuthState>((set, get) => ({
  accessToken: localStorage.getItem(ACCESS_KEY),
  refreshToken: localStorage.getItem(REFRESH_KEY),
  user: null,
  isLoading: false,

  login: async (email, password, mfaCode) => {
    set({ isLoading: true })
    try {
      const tokens = await api.login(email, password, mfaCode)
      persist(tokens)
      set({ accessToken: tokens.access_token, refreshToken: tokens.refresh_token })
      await get().loadProfile()
    } finally {
      set({ isLoading: false })
    }
  },

  logout: () => {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
    set({ accessToken: null, refreshToken: null, user: null })
  },

  loadProfile: async () => {
    if (!get().accessToken) return
    const user = await api.me()
    set({ user })
  },
}))

setTokenReader(() => useAuthStore.getState().accessToken)
