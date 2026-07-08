<script setup>
import { reactive } from 'vue'
import { useRouter } from 'vue-router'
import { Button, FormControl, toast, FeatherIcon } from 'frappe-ui'
import { useLogin } from '../stores/auth'

const router = useRouter()
const { login, loading, error: loginError } = useLogin()
const form = reactive({ username: '', password: '' })
const error = reactive({ message: '' })

async function handleLogin() {
  error.message = ''
  try {
    await login(form.username, form.password)
    toast.success('Welcome back!')
    router.push({ name: 'Dashboard' })
  } catch (e) {
    error.message = e.message
  }
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

      <!-- Login form -->
      <form
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

      <p class="mt-4 text-center text-p-xs text-ink-gray-5">
        p2 Storage Engine
      </p>
    </div>
  </div>
</template>
