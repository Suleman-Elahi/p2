<script setup>
import { ref, watch } from 'vue'
import { Button, Dialog, Badge, TabButtons, FormControl, toast, FeatherIcon, confirmDialog } from 'frappe-ui'
import { useSettingsSingleton } from '../stores/settings'

const {
  config, configLoading,
  apiKeys, keysLoading, createApiKey, keyCreating, deleteApiKey,
  users, usersLoading, createUser, userCreating,
  policies, policiesLoading, createPolicy, updatePolicy, deletePolicy,
  mfaEnabled, mfaStatusLoading, setupMfa, mfaSettingUp, confirmMfa, mfaConfirming, disableMfa, mfaDisabling,
} = useSettingsSingleton()

const activeTab = ref('keys')

// ── API Key create dialog ──────────────────────────────────────────────────
const showKeyDialog = ref(false)
const keyFormName = ref('')
const newSecret = ref('')

watch(showKeyDialog, (val) => {
  if (val) { keyFormName.value = ''; newSecret.value = '' }
})

async function handleCreateKey(close) {
  try {
    const data = await createApiKey({ name: keyFormName.value })
    newSecret.value = data.secret_key
    toast.success(`API Key "${data.name}" created`)
    keyFormName.value = ''
    close()
  } catch (e) {
    toast.error(e.message)
  }
}

function promptDeleteKey(key) {
  confirmDialog({
    title: 'Delete API Key?',
    message: `Access key "${key.access_key}" will stop working immediately.`,
    theme: 'red',
    confirmLabel: 'Delete',
    onConfirm: async ({ hideDialog }) => {
      await deleteApiKey(key.id)
      toast.success(`API Key "${key.name}" deleted`)
      hideDialog()
    },
  })
}

// ── Policy create/edit dialog ──────────────────────────────────────────────
const showPolicyDialog = ref(false)
const policyEditing = ref(null)
const policyName = ref('')
const policyMatchPath = ref('')
const policyMatchHost = ref('')
const policySaving = ref(false)

watch(showPolicyDialog, (val) => {
  if (!val) { policyEditing.value = null }
})

function openCreatePolicy() {
  policyEditing.value = null
  policyName.value = ''
  policyMatchPath.value = ''
  policyMatchHost.value = ''
  showPolicyDialog.value = true
}

function openEditPolicy(policy) {
  policyEditing.value = policy
  policyName.value = policy.name || ''
  policyMatchPath.value = policy.tags?.TAG_SERVE_MATCH_PATH || ''
  policyMatchHost.value = policy.tags?.TAG_SERVE_MATCH_HOST || ''
  showPolicyDialog.value = true
}

async function handleSavePolicy(close) {
  policySaving.value = true
  try {
    const payload = {
      name: policyName.value,
      tags: {
        TAG_SERVE_MATCH_PATH: policyMatchPath.value,
        TAG_SERVE_MATCH_HOST: policyMatchHost.value,
      },
    }
    if (policyEditing.value) {
      await updatePolicy(policyEditing.value.id, payload)
      toast.success('Policy updated')
    } else {
      await createPolicy(payload)
      toast.success('Policy created')
    }
    close()
  } catch (e) {
    toast.error(e.message)
  } finally {
    policySaving.value = false
  }
}

function promptDeletePolicy(policy) {
  confirmDialog({
    title: 'Delete Policy?',
    message: `Are you sure you want to permanently remove "${policy.name || 'Unnamed'}"?`,
    theme: 'red',
    confirmLabel: 'Delete',
    onConfirm: async ({ hideDialog }) => {
      await deletePolicy(policy.id)
      toast.success('Policy deleted')
      hideDialog()
    },
  })
}

// ── User creation dialog ──────────────────────────────────────────────────
const showUserDialog = ref(false)
const userName = ref('')
const userPassword = ref('')
const userEmail = ref('')
const userIsAdmin = ref(false)

watch(showUserDialog, (val) => {
  if (val) { userName.value = ''; userPassword.value = ''; userEmail.value = ''; userIsAdmin.value = false }
})

async function handleCreateUser(close) {
  if (!userName.value.trim() || !userPassword.value.trim()) return
  try {
    await createUser({
      username: userName.value.trim(),
      password: userPassword.value,
      email: userEmail.value.trim(),
      is_superuser: userIsAdmin.value,
    })
    toast.success(`User "${userName.value}" created`)
    close()
  } catch (e) {
    toast.error(e.message || 'Failed to create user')
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────
function copyToClipboard(text) {
  navigator.clipboard.writeText(text)
  toast.success('Copied to clipboard')
}

// ── MFA (TOTP 2FA) enrollment dialog ────────────────────────────────────────
const showMfaSetupDialog = ref(false)
const mfaSetupData = ref(null) // { secret, otpauth_url, qr_svg }
const mfaConfirmCode = ref('')
const mfaRecoveryCodes = ref(null)

async function startMfaSetup() {
  try {
    mfaSetupData.value = await setupMfa()
    mfaConfirmCode.value = ''
    mfaRecoveryCodes.value = null
    showMfaSetupDialog.value = true
  } catch (e) {
    toast.error(e.message || 'Failed to start 2FA setup')
  }
}

async function handleConfirmMfa() {
  try {
    const result = await confirmMfa(mfaConfirmCode.value)
    mfaRecoveryCodes.value = result.recovery_codes
    toast.success('Two-factor authentication enabled')
  } catch (e) {
    toast.error(e.message || 'Incorrect code')
  }
}

const showMfaDisableDialog = ref(false)
const mfaDisablePassword = ref('')

watch(showMfaDisableDialog, (val) => { if (val) mfaDisablePassword.value = '' })

async function handleDisableMfa(close) {
  try {
    await disableMfa(mfaDisablePassword.value)
    toast.success('Two-factor authentication disabled')
    close()
  } catch (e) {
    toast.error(e.message || 'Incorrect password')
  }
}
</script>

<template>
  <div class="flex h-full flex-col">
    <!-- Header -->
    <header class="sticky top-0 z-10 flex min-h-12 items-center justify-between border-b border-outline-gray-1 bg-surface-white px-3 sm:px-5">
      <h1 class="text-2xl font-semibold text-ink-gray-9">Settings</h1>
    </header>

    <!-- Tabs -->
    <nav class="border-b border-outline-gray-1 bg-surface-white px-3 sm:px-5">
      <TabButtons
        v-model="activeTab"
        :buttons="[
          { label: 'API Keys', value: 'keys', icon: 'key' },
          { label: 'Users', value: 'users', icon: 'users' },
          { label: 'Security', value: 'security', icon: 'lock' },
          { label: 'Policies', value: 'policies', icon: 'shield' },
          { label: 'System', value: 'config', icon: 'settings' },
        ]"
      />
    </nav>

    <!-- Content -->
    <div class="flex-1 overflow-auto px-3 pt-5 pb-40 sm:px-5">
      <div class="mx-auto w-full max-w-[800px]">

        <!-- ═══ API Keys ═══ -->
        <div v-if="activeTab === 'keys'" class="space-y-4">
          <div class="flex items-center justify-between">
            <h2 class="text-base font-medium text-ink-gray-8">API Keys</h2>
            <Button variant="solid" theme="gray" icon-left="plus" label="Create Key" @click="showKeyDialog = true" />
          </div>

          <div v-if="keysLoading" class="py-8 text-center text-sm text-ink-gray-5">Loading...</div>

          <div v-else class="divide-y divide-outline-gray-1 rounded-md border border-outline-gray-1 overflow-hidden">
            <div
              v-for="key in apiKeys"
              :key="key.id"
              class="flex items-center justify-between bg-surface-white px-4 py-3"
            >
              <div class="min-w-0">
                <p class="text-sm font-medium text-ink-gray-9">{{ key.name }}</p>
                <p class="text-xs text-ink-gray-5 font-mono">{{ key.access_key }}</p>
              </div>
              <div class="flex items-center gap-2 shrink-0">
                <Button icon="copy" variant="ghost" size="sm" @click="copyToClipboard(key.access_key)" />
                <Button icon="trash-2" variant="ghost" theme="red" size="sm" @click="promptDeleteKey(key)" />
              </div>
            </div>
            <div v-if="!apiKeys.length" class="px-4 py-8 text-center text-p-sm text-ink-gray-5">
              No API keys yet.
            </div>
          </div>

          <!-- New secret display -->
          <div v-if="newSecret" class="rounded-md border border-orange-300 bg-orange-50 p-4">
            <p class="text-sm font-medium text-orange-800 mb-1">Secret Key — shown only once!</p>
            <p class="text-sm font-mono text-orange-700 break-all">{{ newSecret }}</p>
            <div class="mt-2 flex gap-2">
              <Button size="sm" label="Copy" @click="copyToClipboard(newSecret)" />
              <Button size="sm" label="Dismiss" variant="ghost" @click="newSecret = ''" />
            </div>
          </div>
        </div>

        <!-- ═══ Users ═══ -->
        <div v-if="activeTab === 'users'" class="space-y-4">
          <div class="flex items-center justify-between">
            <h2 class="text-base font-medium text-ink-gray-8">Users</h2>
            <Button variant="solid" theme="gray" icon-left="plus" label="Create User" @click="showUserDialog = true" />
          </div>

          <div v-if="usersLoading" class="py-8 text-center text-sm text-ink-gray-5">Loading...</div>

          <div v-else class="divide-y divide-outline-gray-1 rounded-md border border-outline-gray-1 overflow-hidden">
            <div
              v-for="u in users"
              :key="u.id"
              class="flex items-center justify-between bg-surface-white px-4 py-3"
            >
              <div>
                <p class="text-sm font-medium text-ink-gray-9">{{ u.username }}</p>
                <p class="text-xs text-ink-gray-5">{{ u.email || 'No email' }}</p>
              </div>
              <Badge :label="u.is_superuser ? 'Admin' : 'User'" :theme="u.is_superuser ? 'red' : 'gray'" variant="subtle" size="sm" />
            </div>
          </div>
        </div>

        <!-- ═══ Security (2FA) ═══ -->
        <div v-if="activeTab === 'security'" class="space-y-4">
          <h2 class="text-base font-medium text-ink-gray-8">Two-Factor Authentication</h2>
          <p class="text-p-sm text-ink-gray-5">
            Require a time-based one-time password (TOTP) from an authenticator
            app in addition to your password. This is a per-account setting —
            alternatively you can sign in with a social/SSO provider from the
            login page instead of a password + 2FA.
          </p>

          <div v-if="mfaStatusLoading" class="py-8 text-center text-sm text-ink-gray-5">Loading...</div>

          <div v-else class="rounded-md border border-outline-gray-1 bg-surface-white p-4">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <Badge :label="mfaEnabled ? 'Enabled' : 'Disabled'" :theme="mfaEnabled ? 'green' : 'gray'" variant="subtle" size="sm" />
                <span class="text-sm text-ink-gray-7">Authenticator app (TOTP)</span>
              </div>
              <Button
                v-if="!mfaEnabled"
                variant="solid" theme="gray" label="Enable 2FA"
                :loading="mfaSettingUp"
                @click="startMfaSetup"
              />
              <Button
                v-else
                variant="outline" theme="red" label="Disable 2FA"
                @click="showMfaDisableDialog = true"
              />
            </div>
          </div>
        </div>

        <!-- ═══ Policies ═══ -->
        <div v-if="activeTab === 'policies'" class="space-y-4">
          <div class="flex items-center justify-between">
            <h2 class="text-base font-medium text-ink-gray-8">Serve Rules</h2>
            <Button variant="solid" theme="gray" icon-left="plus" label="Add Rule" @click="openCreatePolicy" />
          </div>

          <div v-if="policiesLoading" class="py-8 text-center text-sm text-ink-gray-5">Loading...</div>

          <div v-else class="divide-y divide-outline-gray-1 rounded-md border border-outline-gray-1 overflow-hidden">
            <div
              v-for="p in policies"
              :key="p.id"
              class="flex items-center justify-between bg-surface-white px-4 py-3"
            >
              <div class="min-w-0">
                <p class="text-sm font-medium text-ink-gray-9">{{ p.name || 'Unnamed Rule' }}</p>
                <p class="text-xs text-ink-gray-5">
                  <span v-if="p.tags?.TAG_SERVE_MATCH_PATH">Path: {{ p.tags.TAG_SERVE_MATCH_PATH }}</span>
                  <span v-if="p.tags?.TAG_SERVE_MATCH_HOST"> · Host: {{ p.tags.TAG_SERVE_MATCH_HOST }}</span>
                </p>
              </div>
              <div class="flex items-center gap-2 shrink-0">
                <Button icon="edit" variant="ghost" size="sm" @click="openEditPolicy(p)" />
                <Button icon="trash-2" variant="ghost" theme="red" size="sm" @click="promptDeletePolicy(p)" />
              </div>
            </div>
            <div v-if="!policies.length" class="px-4 py-8 text-center text-p-sm text-ink-gray-5">
              No policies defined yet.
            </div>
          </div>
        </div>

        <!-- ═══ System ═══ -->
        <div v-if="activeTab === 'config'" class="space-y-6">
          <h2 class="text-base font-medium text-ink-gray-8">System Configuration</h2>

          <div v-if="configLoading" class="py-8 text-center text-sm text-ink-gray-5">Loading...</div>

          <div v-else class="space-y-4">
            <div class="rounded-md border border-outline-gray-1 bg-surface-white p-4">
              <p class="text-xs text-ink-gray-5 mb-1">S3 Endpoint</p>
              <p class="text-sm font-mono text-ink-gray-9">{{ config.s3_endpoint || 'localhost' }}</p>
            </div>
            <div class="rounded-md border border-outline-gray-1 bg-surface-white p-4">
              <p class="text-xs text-ink-gray-5 mb-1">Version</p>
              <p class="text-sm font-mono text-ink-gray-9">{{ config.version }}</p>
            </div>
            <div class="rounded-md border border-outline-gray-1 bg-surface-white p-4">
              <p class="text-xs text-ink-gray-5 mb-2">Storage Classes</p>
              <div class="flex flex-wrap gap-2">
                <Badge v-for="sc in config.storage_classes" :key="sc.value" :label="sc.label" theme="gray" variant="subtle" size="sm" />
              </div>
            </div>
            <div class="rounded-md border border-outline-gray-1 bg-surface-white p-4">
              <p class="text-xs text-ink-gray-5 mb-2">Encryption Options</p>
              <div class="flex flex-wrap gap-2">
                <Badge v-for="enc in config.encryption_options" :key="enc.value" :label="enc.label" theme="gray" variant="subtle" size="sm" />
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>

    <!-- ═══ Dialogs ═══ -->

    <!-- Create API Key -->
    <Dialog v-model="showKeyDialog" :key="'key-' + showKeyDialog" :options="{ title: 'Create API Key', icon: { name: 'key' }, size: 'lg' }">
      <template #body-content>
        <div class="space-y-4" @pointerdown.stop>
          <FormControl
            v-model="keyFormName"
            label="Key Name"
            type="text"
            placeholder="My API Key"
            required
          />
        </div>
      </template>
      <template #actions="{ close }">
        <div class="flex justify-end gap-2 w-full">
          <Button label="Cancel" @click="close" />
          <Button
            variant="solid" theme="gray" label="Create"
            :loading="keyCreating" :disabled="!keyFormName.trim()"
            @click="handleCreateKey(close)"
          />
        </div>
      </template>
    </Dialog>

    <!-- Create/Edit Policy -->
    <Dialog v-model="showPolicyDialog" :key="'policy-' + showPolicyDialog" :options="{ title: policyEditing ? 'Edit Policy' : 'Create Policy', icon: { name: 'shield' }, size: 'lg' }">
      <template #body-content>
        <div class="space-y-4" @pointerdown.stop>
          <FormControl v-model="policyName" label="Rule Name" type="text" placeholder="my-rule" />
          <FormControl v-model="policyMatchPath" label="Match Path (regex)" type="text" placeholder="/public/*" />
          <FormControl v-model="policyMatchHost" label="Match Host (regex)" type="text" placeholder="*.example.com" />
        </div>
      </template>
      <template #actions="{ close }">
        <div class="flex justify-end gap-2 w-full">
          <Button label="Cancel" @click="close" />
          <Button
            variant="solid" theme="gray" :label="policyEditing ? 'Update' : 'Create'"
            :loading="policySaving"
            @click="handleSavePolicy(close)"
          />
        </div>
      </template>
    </Dialog>

    <!-- Enable 2FA (QR + confirm) -->
    <Dialog v-model="showMfaSetupDialog" :key="'mfa-setup-' + showMfaSetupDialog" :options="{ title: 'Enable Two-Factor Authentication', icon: { name: 'shield' }, size: 'lg' }">
      <template #body-content>
        <div class="space-y-4" @pointerdown.stop>
          <template v-if="!mfaRecoveryCodes">
            <p class="text-p-sm text-ink-gray-6">
              Scan this QR code with your authenticator app (Google Authenticator,
              Authy, 1Password, etc.), then enter the 6-digit code it generates.
            </p>
            <div v-if="mfaSetupData" class="flex justify-center rounded-md border border-outline-gray-1 bg-white p-4" v-html="mfaSetupData.qr_svg" />
            <p v-if="mfaSetupData" class="text-center text-p-xs font-mono text-ink-gray-5 break-all">
              {{ mfaSetupData.secret }}
            </p>
            <FormControl
              v-model="mfaConfirmCode"
              label="6-digit code"
              type="text"
              placeholder="123456"
              autocomplete="one-time-code"
            />
          </template>
          <template v-else>
            <p class="text-p-sm font-medium text-ink-gray-9">
              Save these recovery codes somewhere safe — each can be used once
              to sign in if you lose access to your authenticator app. They
              will not be shown again.
            </p>
            <div class="grid grid-cols-2 gap-2 rounded-md border border-outline-gray-1 bg-surface-gray-1 p-4 font-mono text-sm">
              <span v-for="rc in mfaRecoveryCodes" :key="rc">{{ rc }}</span>
            </div>
            <Button label="Copy all" @click="copyToClipboard(mfaRecoveryCodes.join('\n'))" />
          </template>
        </div>
      </template>
      <template #actions="{ close }">
        <div class="flex justify-end gap-2 w-full">
          <Button v-if="!mfaRecoveryCodes" label="Cancel" @click="close" />
          <Button
            v-if="!mfaRecoveryCodes"
            variant="solid" theme="gray" label="Verify & Enable"
            :loading="mfaConfirming" :disabled="!mfaConfirmCode.trim()"
            @click="handleConfirmMfa"
          />
          <Button v-else variant="solid" theme="gray" label="Done" @click="close" />
        </div>
      </template>
    </Dialog>

    <!-- Disable 2FA -->
    <Dialog v-model="showMfaDisableDialog" :key="'mfa-disable-' + showMfaDisableDialog" :options="{ title: 'Disable Two-Factor Authentication', icon: { name: 'shield-off' }, size: 'lg' }">
      <template #body-content>
        <div class="space-y-4" @pointerdown.stop>
          <p class="text-p-sm text-ink-gray-6">
            Confirm your password to turn off two-factor authentication.
          </p>
          <FormControl
            v-model="mfaDisablePassword"
            type="password"
            label="Password"
            placeholder="••••••••"
            required
          />
        </div>
      </template>
      <template #actions="{ close }">
        <div class="flex justify-end gap-2 w-full">
          <Button label="Cancel" @click="close" />
          <Button
            variant="solid" theme="red" label="Disable 2FA"
            :loading="mfaDisabling" :disabled="!mfaDisablePassword.trim()"
            @click="handleDisableMfa(close)"
          />
        </div>
      </template>
    </Dialog>

    <!-- Create User -->
    <Dialog v-model="showUserDialog" :key="'user-' + showUserDialog" :options="{ title: 'Create User', icon: { name: 'user-plus' }, size: 'lg' }">
      <template #body-content>
        <div class="space-y-4" @pointerdown.stop>
          <FormControl
            v-model="userName"
            label="Username"
            type="text"
            placeholder="jdoe"
            required
          />
          <FormControl
            v-model="userPassword"
            type="password"
            label="Password"
            placeholder="••••••••"
            required
          />
          <FormControl
            v-model="userEmail"
            label="Email"
            type="text"
            placeholder="jdoe@example.com"
          />
          <FormControl
            v-model="userIsAdmin"
            type="checkbox"
            label="Superuser (admin)"
          />
        </div>
      </template>
      <template #actions="{ close }">
        <div class="flex justify-end gap-2 w-full">
          <Button label="Cancel" @click="close" />
          <Button
            variant="solid" theme="gray" label="Create User"
            :loading="userCreating" :disabled="!userName.trim() || !userPassword.trim()"
            @click="handleCreateUser(close)"
          />
        </div>
      </template>
    </Dialog>
  </div>
</template>
