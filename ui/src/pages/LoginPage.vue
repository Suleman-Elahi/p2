<script setup>
import { reactive, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Button, FormControl, toast, FeatherIcon } from 'frappe-ui'
import { useLogin, useMfaVerify } from '../stores/auth'
import { saveTokens, decodeUserFromToken } from '../stores/api'

const router = useRouter()
const { login, loading, error: loginError } = useLogin()
const { verify: verifyMfa, loading: verifyingMfa } = useMfaVerify()
const form = reactive({ username: '', password: '' })
const error = reactive({ message: '' })

// Second step: shown only when /auth/login reports the account has TOTP
// 2FA enabled. mfaToken binds this code entry to the just-verified password.
const mfaStep = ref(false)
const mfaToken = ref('')
const mfaCode = ref('')

// Providers enabled for this deployment. Configure credentials for each via
// /_/admin/socialaccount/socialapp/ — adding a provider there (and to
// INSTALLED_APPS server-side) is the only step needed; this list should be
// kept in sync with what's actually configured so users don't see dead buttons.
const ssoProviders = [
  { id: 'google', label: 'Google', icon: 'chrome' },
  { id: 'microsoft', label: 'Microsoft', icon: 'square' },
  { id: 'github', label: 'GitHub', icon: 'github' },
]

function loginWithProvider(providerId) {
  // Full page navigation is required here (not a fetch/XHR) — the browser
  // must actually visit the provider's consent screen and come back.
  window.location.href = `/_/accounts/${providerId}/login/`
}

// After a successful SSO round trip, P2AccountAdapter redirects back to
// /login?access=...&refresh=... — pick the tokens up here the same way
// useLogin() does for password auth, then continue into the app.
onMounted(() => {
  const params = new URLSearchParams(window.location.search)
  const access = params.get('access')
  const refresh = params.get('refresh')
  if (access && refresh) {
    saveTokens(access, refresh)
    decodeUserFromToken(access)
    toast.success('Welcome back!')
    router.replace({ name: 'Dashboard' })
  }
})

async function handleLogin() {
  error.message = ''
  try {
    const result = await login(form.username, form.password)
    if (result.mfaRequired) {
      mfaToken.value = result.mfaToken
      mfaStep.value = true
      return
    }
    toast.success('Welcome back!')
    router.push({ name: 'Dashboard' })
  } catch (e) {
    error.message = e.message
  }
}

async function handleMfaVerify() {
  error.message = ''
  try {
    await verifyMfa(mfaToken.value, mfaCode.value)
    toast.success('Welcome back!')
    router.push({ name: 'Dashboard' })
  } catch (e) {
    error.message = e.message
  }
}

function cancelMfa() {
  mfaStep.value = false
  mfaToken.value = ''
  mfaCode.value = ''
  error.message = ''
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center bg-surface-gray-1 px-4">
    <div class="w-full max-w-sm">
      <!-- Logo / branding -->
      <div class="mb-8 text-center">
        <div class="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-xl bg-surface-white border border-outline-gray-1 shadow-sm">
          <FeatherIcon name="hard-drive" class="h-7 w-7 text-ink-gray-7" />
        </div>
        <h1 class="text-2xl font-semibold text-ink-gray-9">P2 Storage Manager</h1>
        <p class="mt-1 text-p-sm text-ink-gray-5">Sign in to manage your object storage</p>
      </div>

      <!-- Step 2: TOTP / recovery code (only shown after password auth
           reports the account has 2FA enabled) -->
      <form
        v-if="mfaStep"
        class="space-y-4 rounded-lg border border-outline-gray-1 bg-surface-white p-6 shadow-sm"
        @submit.prevent="handleMfaVerify"
      >
        <div class="text-center">
          <FeatherIcon name="shield" class="mx-auto mb-2 h-6 w-6 text-ink-gray-7" />
          <p class="text-p-base font-medium text-ink-gray-9">Two-factor authentication</p>
          <p class="mt-1 text-p-sm text-ink-gray-5">
            Enter the 6-digit code from your authenticator app, or a recovery code.
          </p>
        </div>
        <FormControl
          v-model="mfaCode"
          label="Authentication code"
          type="text"
          placeholder="123456"
          autocomplete="one-time-code"
          required
        />

        <p v-if="error.message" class="text-p-sm text-ink-red-6">{{ error.message }}</p>

        <Button
          variant="solid"
          theme="gray"
          type="submit"
          :loading="verifyingMfa"
          label="Verify"
          class="w-full"
        />
        <Button variant="ghost" theme="gray" class="w-full" label="Back" @click="cancelMfa" />
      </form>

      <!-- Step 1: username + password -->
      <form
        v-else
        class="space-y-4 rounded-lg border border-outline-gray-1 bg-surface-white p-6 shadow-sm"
        @submit.prevent="handleLogin"
      >
        <FormControl
          v-model="form.username"
          label="Username"
          type="text"
          placeholder="Enter your username"
          required
        />
        <FormControl
          v-model="form.password"
          type="password"
          label="Password"
          placeholder="Enter your password"
          required
        />

        <p v-if="error.message" class="text-p-sm text-ink-red-6">{{ error.message }}</p>

        <Button
          variant="solid"
          theme="gray"
          type="submit"
          :loading="loading"
          label="Sign In"
          class="w-full"
        />
      </form>

      <div v-if="!mfaStep && ssoProviders.length" class="mt-4 space-y-2">
        <div class="flex items-center gap-2">
          <div class="h-px flex-1 bg-outline-gray-2" />
          <span class="text-p-xs text-ink-gray-5">or continue with</span>
          <div class="h-px flex-1 bg-outline-gray-2" />
        </div>
        <Button
          v-for="provider in ssoProviders"
          :key="provider.id"
          variant="outline"
          theme="gray"
          class="w-full"
          @click="loginWithProvider(provider.id)"
        >
          <template #prefix>
            <FeatherIcon :name="provider.icon" class="h-4 w-4" />
          </template>
          {{ provider.label }}
        </Button>
      </div>

      <p class="mt-4 text-center text-p-xs text-ink-gray-5">
        p2 Storage Engine
      </p>
    </div>
  </div>
</template>
