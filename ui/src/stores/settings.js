import { useApi } from './api'
import { computed, ref } from 'vue'

// ── Shared config ────────────────────────────────────────────────────────
const config = ref({
  s3_endpoint: '',
  storage_classes: [],
  encryption_options: [],
  region_options: [],
  version: '',
})
const apiKeys = ref([])
const users = ref([])
const policies = ref([])

// ── Composable: all settings ─────────────────────────────────────────────

export function useSettings() {
  // ── System Config ────────────────────────────────────────────────────
  const { data: cfgData, isFetching: cfgLoading, execute: fetchCfg } = useApi(
    '/system/config/',
    { refetch: true },
  ).json()

  const cfg = computed(() => {
    if (cfgData.value) config.value = cfgData.value
    return config.value
  })

  // ── API Keys ──────────────────────────────────────────────────────────
  const { data: keysData, isFetching: keysLoading, execute: fetchKeys } = useApi(
    '/system/key/',
    { refetch: true },
  ).json()

  const keys = computed(() => {
    if (keysData.value) apiKeys.value = keysData.value
    return apiKeys.value
  })

  // Create
  const keyCreateBody = ref({})
  const { data: keyCreateData, execute: keyCreate, isFetching: keyCreating, error: keyCreateError } = useApi(
    '/system/key/',
    { immediate: false },
  ).post(keyCreateBody).json()

  async function createApiKey(payload) {
    keyCreateBody.value = payload
    await keyCreate()
    if (keyCreateError.value) throw keyCreateError.value
    await fetchKeys()
    return keyCreateData.value
  }

  // Delete
  const keyDeleting = ref(false)
  const keyDelError = ref(null)

  async function deleteApiKey(id) {
    keyDeleting.value = true
    keyDelError.value = null
    try {
      const token = localStorage.getItem('p2_token') || ''
      const resp = await fetch(`/api/v1/system/key/${id}/`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}))
        throw new Error(body.detail || `HTTP ${resp.status}`)
      }
      await fetchKeys()
    } catch (e) {
      keyDelError.value = e
      throw e
    } finally {
      keyDeleting.value = false
    }
  }

  // ── Users ──────────────────────────────────────────────────────────────
  const { data: usersData, isFetching: usersLoading, execute: fetchUsers } = useApi(
    '/system/user/',
    { refetch: true },
  ).json()

  const usersList = computed(() => {
    if (usersData.value) users.value = usersData.value
    return users.value
  })

  // Create
  const userCreateBody = ref({})
  const { execute: userCreate, isFetching: userCreating, error: userCreateError } = useApi(
    '/system/user/',
    { immediate: false },
  ).post(userCreateBody).json()

  async function createUser(payload) {
    userCreateBody.value = {
      username: payload.username,
      password: payload.password,
      email: payload.email,
      is_superuser: payload.is_superuser,
    }
    await userCreate()
    if (userCreateError.value) throw userCreateError.value
    await fetchUsers()
  }

  // ── Policies ───────────────────────────────────────────────────────────
  const { data: policiesData, isFetching: policiesLoading, execute: fetchPolicies } = useApi(
    '/tier0/policy/',
    { refetch: true },
  ).json()

  const policiesList = computed(() => {
    if (policiesData.value) policies.value = policiesData.value
    return policies.value
  })

  // Create
  const policyCreateBody = ref({})
  const { execute: policyCreate, isFetching: policyCreating, error: policyCreateError } = useApi(
    '/tier0/policy/',
    { immediate: false },
  ).post(policyCreateBody).json()

  async function createPolicy(payload) {
    policyCreateBody.value = payload
    await policyCreate()
    if (policyCreateError.value) throw policyCreateError.value
    await fetchPolicies()
  }

  // Update
  const policyUpdateBody = ref({})
  const { execute: policyUpdate, isFetching: policyUpdating, error: policyUpdateError } = useApi(
    (args) => args ? `/tier0/policy/${args.id}/` : '/tier0/policy/_placeholder/',
    { immediate: false },
  ).put(policyUpdateBody).json()

  async function updatePolicy(id, payload) {
    policyUpdateBody.value = payload
    await policyUpdate({ id })
    if (policyUpdateError.value) throw policyUpdateError.value
    await fetchPolicies()
  }

  // Delete
  const policyDeleting = ref(false)
  const policyDelError = ref(null)

  async function deletePolicy(id) {
    policyDeleting.value = true
    policyDelError.value = null
    try {
      const token = localStorage.getItem('p2_token') || ''
      const resp = await fetch(`/api/v1/tier0/policy/${id}/`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}))
        throw new Error(body.detail || `HTTP ${resp.status}`)
      }
      await fetchPolicies()
    } catch (e) {
      policyDelError.value = e
      throw e
    } finally {
      policyDeleting.value = false
    }
  }

  return {
    // Config
    config: cfg,
    configLoading: cfgLoading,
    fetchConfig: fetchCfg,
    // API Keys
    apiKeys: keys,
    keysLoading,
    fetchApiKeys: fetchKeys,
    createApiKey,
    keyCreating,
    deleteApiKey,
    keyDeleting,
    // Users
    users: usersList,
    usersLoading,
    fetchUsers,
    createUser,
    userCreating,
    // Policies
    policies: policiesList,
    policiesLoading,
    fetchPolicies,
    createPolicy,
    policyCreating,
    updatePolicy,
    policyUpdating,
    deletePolicy,
    policyDeleting,
  }
}

// ── Default singleton ────────────────────────────────────────────────────
let defaultInstance = null

export function useSettingsSingleton() {
  if (!defaultInstance) defaultInstance = useSettings()
  return defaultInstance
}

export { config, apiKeys, users, policies }
