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
//
// Login goes through /api/v1/auth/login rather than the raw ninja-jwt
// /api/v1/auth/token/pair endpoint, because only /auth/login knows to hold
// back the JWT pair and return an MFA challenge when the user has TOTP
// two-factor auth enabled (see p2/auth/mfa_api.py). If the account has no
// 2FA, the response shape is the same tokens as before, just one hop later.

export function useLogin() {
  const loginBody = ref({ username: '', password: '' })

  const { data, isFetching, error, execute } = useFetch('/api/v1/auth/login', {
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
    if (result?.mfa_required) {
      // Caller (LoginPage.vue) must prompt for a TOTP/recovery code and
      // call useMfaVerify().verify(result.mfa_token, code) to finish login.
      return { mfaRequired: true, mfaToken: result.mfa_token }
    }
    if (result?.access) {
      saveTokens(result.access, result.refresh)
      decodeUserFromToken(result.access)
    }
    return { mfaRequired: false, user: user.value }
  }

  return { login, loading: isFetching, error }
}

// ── MFA challenge verification (second factor) ──────────────────────────

export function useMfaVerify() {
  const verifyBody = ref({ mfa_token: '', code: '' })

  const { data, isFetching, error, execute } = useFetch('/api/v1/auth/mfa/verify', {
    immediate: false,
    beforeFetch({ options }) {
      return { options }
    },
  }).post(verifyBody).json()

  async function verify(mfaToken, code) {
    error.value = null
    verifyBody.value = { mfa_token: mfaToken, code }
    await execute()
    if (error.value) throw error.value
    const result = data.value
    if (result?.access) {
      saveTokens(result.access, result.refresh)
      decodeUserFromToken(result.access)
    }
    return user.value
  }

  return { verify, loading: isFetching, error }
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
      setUser(null)
      return false
    }
    if (data.value?.access) {
      saveTokens(data.value.access, localStorage.getItem('p2_refresh') || '')
      decodeUserFromToken(data.value.access)
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
