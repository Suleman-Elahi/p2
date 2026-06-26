import { useFetch } from '@vueuse/core'
import { computed, ref } from 'vue'
import {
  getAccessToken, saveTokens, clearTokens,
  isAuthenticated, getUser, setUser, decodeUserFromToken,
} from './api'

// ── User (shared reactive — set by login / token decode) ────────────────
const user = getUser()

// ── Login ────────────────────────────────────────────────────────────────
// useFetch for login — uses a reactive ref for the POST body so execute()
// sends the correct payload without destructuring issues.

export function useLogin() {
  const loginBody = ref({ username: '', password: '' })

  const { data, isFetching, error, execute } = useFetch('/api/v1/auth/token/pair', {
    immediate: false,
    beforeFetch({ options }) {
      // login doesn't need auth header
      return { options }
    },
  }).post(loginBody).json()

  async function login(username, password) {
    // Clear any previous error and set the request body
    error.value = null
    loginBody.value = { username, password }
    await execute()
    if (error.value) throw error.value
    const result = data.value
    if (result?.access) {
      saveTokens(result.access, result.refresh)
      decodeUserFromToken(result.access)
    }
    return user.value
  }

  return { login, loading: isFetching, error }
}

// ── Token refresh ────────────────────────────────────────────────────────

export function useRefresh() {
  const refreshBody = ref({ refresh: '' })

  const { data, isFetching, error, execute } = useFetch('/api/v1/auth/token/refresh', {
    immediate: false,
    beforeFetch({ options }) {
      return { options }
    },
  }).post(refreshBody).json()

  async function refresh() {
    error.value = null
    refreshBody.value = { refresh: localStorage.getItem('p2_refresh') || '' }
    await execute()
    if (error.value) {
      clearTokens()
      return false
    }
    if (data.value?.access) {
      saveTokens(data.value.access, localStorage.getItem('p2_refresh') || '')
      return true
    }
    return false
  }

  return { refresh, loading: isFetching, error }
}

// ── Logout ───────────────────────────────────────────────────────────────

export function logout() {
  clearTokens()
  setUser(null)
  window.location.href = '/login'
}

export { user, isAuthenticated, getAccessToken }
