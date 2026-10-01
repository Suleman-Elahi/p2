import { createFetch } from '@vueuse/core'
import { ref } from 'vue'

// ── Shared auth token state ─────────────────────────────────────────────
const accessToken = ref(localStorage.getItem('p2_token') || '')
const refreshToken = ref(localStorage.getItem('p2_refresh') || '')

export function getAccessToken() { return accessToken.value }
export function getRefreshToken() { return refreshToken.value }

export function saveTokens(access, refresh) {
  accessToken.value = access
  refreshToken.value = refresh
  localStorage.setItem('p2_token', access)
  localStorage.setItem('p2_refresh', refresh)
}

export function clearTokens() {
  accessToken.value = ''
  refreshToken.value = ''
  localStorage.removeItem('p2_token')
  localStorage.removeItem('p2_refresh')
}

// ── Shared user state (decoded from JWT) ────────────────────────────────
import { computed } from 'vue'

const user = ref(null)

export const isAuthenticated = computed(() => !!accessToken.value && !!user.value)

export function setUser(u) {
  user.value = u
}

export function getUser() {
  return user
}

export function decodeUserFromToken(token) {
  try {
    const payload = JSON.parse(atob(token.split('.')[1]))
    if (payload.exp && payload.exp * 1000 < Date.now()) {
      user.value = null
      return null
    }
    const isSuperuser = Boolean(payload.is_superuser)
    user.value = {
      id: payload.user_id,
      username: payload.username || payload.sub || payload.user_id,
      email: payload.email || '',
      is_superuser: isSuperuser,
      is_staff: Boolean(payload.is_staff),
      role: isSuperuser ? 'admin' : 'member',
    }
    return user.value
  } catch {
    user.value = null
    return null
  }
}

export async function fetchCurrentUser() {
  const token = accessToken.value
  if (!token) return null
  try {
    const resp = await fetch('/api/v1/auth/me', {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (resp.ok) {
      const data = await resp.json()
      user.value = {
        ...user.value,
        id: data.id,
        username: data.username,
        email: data.email,
        is_superuser: Boolean(data.is_superuser),
        is_staff: Boolean(data.is_staff),
        role: data.is_superuser ? 'admin' : 'member',
      }
      return user.value
    }
  } catch {
    // Ignore network error on boot
  }
  return user.value
}

// Restore session from stored token on load
if (accessToken.value) {
  const decoded = decodeUserFromToken(accessToken.value)
  if (!decoded) {
    clearTokens()
  } else {
    fetchCurrentUser()
  }
}

// ── API client (useFetch factory) ───────────────────────────────────────

export const useApi = createFetch({
  baseUrl: '/api/v1',
  options: {
    beforeFetch({ options }) {
      const token = accessToken.value
      if (token) {
        options.headers = {
          ...options.headers,
          Authorization: `Bearer ${token}`,
        }
      }
      // Only set Content-Type for requests with a body, and never for FormData
      if (options.body && !(options.body instanceof FormData)) {
        options.headers = {
          'Content-Type': 'application/json',
          ...options.headers,
        }
      }
      return { options }
    },
    afterFetch(ctx) {
      // If 401 and we have a refresh token, try to refresh
      if (ctx.response.status === 401 && refreshToken.value) {
        // We can't easily retry inside afterFetch, so mark for refresh
        // The caller can handle the 401. For auto-refresh, see the
        // useAuthRefresh composable below.
      }
      return ctx
    },
    onFetchError(ctx) {
      if (ctx.response?.status === 401) {
        clearTokens()
        setUser(null)
        if (window.location.pathname !== '/login') {
          window.location.href = '/login'
        }
      }
      // Parse error body for a detail message
      if (ctx.response?.status) {
        return ctx.response.json().then(body => {
          ctx.error = new Error(body.detail || body.message || `HTTP ${ctx.response.status}`)
          return ctx
        }).catch(() => {
          ctx.error = new Error(`HTTP ${ctx.response.status}`)
          return ctx
        })
      }
      return ctx
    },
  },
})
