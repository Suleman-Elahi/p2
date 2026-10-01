<script setup>
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Badge, Dialog, FormControl, toast, FeatherIcon, confirmDialog } from 'frappe-ui'
import { useBucketsSingleton } from '../stores/buckets'
import { isSuperAdmin } from '../stores/auth'

const router = useRouter()
const { buckets, createBucket, creating, deleteBucket, deleting, updateBucket, updating, selectBucket } = useBucketsSingleton()

const showCreate = ref(false)
const bucketName = ref('')
const bucketVersioning = ref(false)
const bucketEncryption = ref('AES-256')
const bucketAccessPolicy = ref('private')

const showEdit = ref(false)
const editBucketRef = ref(null)
const editVersioning = ref(false)
const editEncryption = ref('AES-256')
const editAccessPolicy = ref('private')

watch(showCreate, (val) => {
  if (val) {
    bucketName.value = ''
    bucketVersioning.value = false
    bucketEncryption.value = 'AES-256'
    bucketAccessPolicy.value = 'private'
  }
})

const encryptionOptions = [
  { label: 'SSE-S3 (AES-256)', value: 'AES-256' },
  { label: 'SSE-KMS (aws:kms)', value: 'aws:kms' },
  { label: 'None', value: 'none' },
]

const accessOptions = [
  { label: 'Private', value: 'private' },
  { label: 'Public Read', value: 'public-read' },
  { label: 'Public Read-Write', value: 'public-read-write' },
]

async function handleCreate(close) {
  if (!bucketName.value.trim()) return
  try {
    await createBucket({
      name: bucketName.value,
      versioning: bucketVersioning.value,
      encryption: bucketEncryption.value,
      accessPolicy: bucketAccessPolicy.value,
    })
    toast.success(`Bucket "${bucketName.value}" created`)
    close()
  } catch (e) {
    toast.error(e.message || 'Failed to create bucket')
  }
}

function openEditBucket(bucket) {
  editBucketRef.value = bucket
  editVersioning.value = bucket.versioning
  editEncryption.value = bucket.encryption
  editAccessPolicy.value = bucket.accessPolicy
  showEdit.value = true
}

async function handleUpdate(close) {
  if (!editBucketRef.value) return
  try {
    await updateBucket(editBucketRef.value.uuid, {
      versioning: editVersioning.value,
      encryption: editEncryption.value,
      accessPolicy: editAccessPolicy.value,
    })
    toast.success(`Bucket "${editBucketRef.value.name}" settings updated`)
    close()
  } catch (e) {
    toast.error(e.message || 'Failed to update bucket')
  }
}

function promptDeleteBucket(bucket) {
  confirmDialog({
    title: 'Delete Bucket?',
    message: `Are you sure you want to delete "${bucket.name}"? All objects in this bucket will be permanently deleted. This cannot be undone.`,
    theme: 'red',
    confirmLabel: 'Delete',
    onConfirm: async ({ hideDialog }) => {
      await deleteBucket(bucket.uuid)
      toast.success(`Bucket "${bucket.name}" deleted`)
      hideDialog()
    },
  })
}

function openBucket(bucket) {
  selectBucket(bucket)
  router.push(`/buckets/${bucket.name}`)
}

function accessTheme(policy) {
  if (policy === 'private') return 'gray'
  if (policy === 'public-read') return 'orange'
  return 'red'
}

// ── Bucket Access Control (ACL) ──────────────────────────────────────────
const showAcl = ref(false)
const selectedBucket = ref(null)
const bucketAcls = ref([])
const aclsLoading = ref(false)
const allUsers = ref([])
const allGroups = ref([])

// Grant Access form
const showGrantForm = ref(false)
const grantType = ref('user')
const grantUserId = ref('')
const grantGroupId = ref('')
const grantPerms = ref(['read', 'list'])
const grantingAcl = ref(false)

async function openAclDialog(bucket) {
  selectedBucket.value = bucket
  showAcl.value = true
  showGrantForm.value = false
  await fetchBucketAcls(bucket.uuid)
  fetchGrantees()
}

async function fetchBucketAcls(uuid) {
  aclsLoading.value = true
  try {
    const token = localStorage.getItem('p2_token') || ''
    const resp = await fetch(`/api/v1/core/volumes/${uuid}/acl/`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (resp.ok) {
      bucketAcls.value = await resp.json()
    } else {
      bucketAcls.value = []
    }
  } catch (e) {
    bucketAcls.value = []
  } finally {
    aclsLoading.value = false
  }
}

async function fetchGrantees() {
  if (!selectedBucket.value) return
  const token = localStorage.getItem('p2_token') || ''
  try {
    const resp = await fetch(
      `/api/v1/core/volumes/${selectedBucket.value.uuid}/acl/grantables/`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
    if (resp.ok) {
      const data = await resp.json()
      allUsers.value = data.users || []
      allGroups.value = data.groups || []
    } else {
      allUsers.value = []
      allGroups.value = []
    }
  } catch {
    allUsers.value = []
    allGroups.value = []
  }
}

function openGrantAccess() {
  grantType.value = 'user'
  grantUserId.value = allUsers.value?.[0]?.id ? String(allUsers.value[0].id) : ''
  grantGroupId.value = allGroups.value?.[0]?.id ? String(allGroups.value[0].id) : ''
  grantPerms.value = ['read', 'list']
  showGrantForm.value = true
}

async function handleGrantAccess() {
  if (grantType.value === 'user' && !grantUserId.value) {
    toast.error('Please select a user')
    return
  }
  if (grantType.value === 'group' && !grantGroupId.value) {
    toast.error('Please select a group')
    return
  }
  if (!grantPerms.value.length) {
    toast.error('Please select at least one permission')
    return
  }

  grantingAcl.value = true
  try {
    const token = localStorage.getItem('p2_token') || ''
    const payload = {
      user_id: grantType.value === 'user' ? Number(grantUserId.value) : null,
      group_id: grantType.value === 'group' ? Number(grantGroupId.value) : null,
      permissions: grantPerms.value,
    }
    const resp = await fetch(`/api/v1/core/volumes/${selectedBucket.value.uuid}/acl/`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
    })
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}))
      throw new Error(body.detail || `HTTP ${resp.status}`)
    }
    toast.success('Access granted')
    showGrantForm.value = false
    await fetchBucketAcls(selectedBucket.value.uuid)
  } catch (e) {
    toast.error(e.message || 'Failed to grant access')
  } finally {
    grantingAcl.value = false
  }
}

function promptRevokeAcl(acl) {
  confirmDialog({
    title: 'Revoke Access?',
    message: `Revoke permissions for ${acl.username ? `user "${acl.username}"` : `group "${acl.group_name}"`} on bucket "${selectedBucket.value.name}"?`,
    theme: 'red',
    confirmLabel: 'Revoke',
    onConfirm: async ({ hideDialog }) => {
      try {
        const token = localStorage.getItem('p2_token') || ''
        const resp = await fetch(`/api/v1/core/volumes/${selectedBucket.value.uuid}/acl/${acl.id}/`, {
          method: 'DELETE',
          headers: { Authorization: `Bearer ${token}` },
        })
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}))
          throw new Error(body.detail || `HTTP ${resp.status}`)
        }
        toast.success('Access revoked')
        hideDialog()
        await fetchBucketAcls(selectedBucket.value.uuid)
      } catch (e) {
        toast.error(e.message || 'Failed to revoke access')
      }
    },
  })
}
</script>

<template>
  <div class="flex h-full flex-col">
    <!-- Header -->
    <header class="sticky top-0 z-10 flex min-h-12 items-center justify-between border-b border-outline-gray-1 bg-surface-white px-3 sm:px-5">
      <h1 class="text-2xl font-semibold text-ink-gray-9">Buckets</h1>
      <Button
        v-if="isSuperAdmin"
        variant="solid"
        theme="gray"
        icon-left="database"
        label="Create Bucket"
        @click="showCreate = true"
      />
    </header>

    <!-- Content -->
    <div class="mx-auto w-full max-w-[940px] px-3 pt-5 pb-40 sm:px-5">
      <!-- Bucket list -->
      <div v-if="buckets.length" class="divide-y divide-outline-gray-1 rounded-md border border-outline-gray-1 overflow-hidden">
        <div
          v-for="b in buckets"
          :key="b.name"
          class="flex items-center justify-between px-4 py-3 bg-surface-white hover:bg-surface-gray-1 transition-colors"
        >
          <button class="flex items-center gap-3 min-w-0 text-left" @click="openBucket(b)">
            <div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-surface-gray-2">
              <FeatherIcon name="database" class="h-4 w-4 text-ink-gray-6" />
            </div>
            <div class="min-w-0">
              <p class="text-base font-medium text-ink-gray-9">{{ b.name }}</p>
              <p class="text-xs text-ink-gray-5">{{ b.object_count }} objects · {{ b.region }}</p>
            </div>
          </button>
          <div class="flex items-center gap-2 shrink-0">
            <Badge v-if="b.versioning" label="Versioned" theme="blue" variant="subtle" size="sm" />
            <Badge :label="b.accessPolicy" :theme="accessTheme(b.accessPolicy)" variant="subtle" size="sm" />
            <Badge :label="b.encryption" theme="gray" variant="subtle" size="sm" />
            <Button v-if="b.canAdmin" icon="shield" variant="ghost" theme="gray" size="sm" title="Bucket Permissions" @click.stop="openAclDialog(b)" />
            <Button v-if="b.canAdmin" icon="settings" variant="ghost" theme="gray" size="sm" @click.stop="openEditBucket(b)" />
            <Button v-if="b.canAdmin" icon="trash-2" variant="ghost" theme="red" size="sm" @click.stop="promptDeleteBucket(b)" />
            <Button icon="chevron-right" variant="ghost" size="sm" @click="openBucket(b)" />
          </div>
        </div>
      </div>

      <!-- Empty state -->
      <div v-else class="flex flex-col items-center justify-center gap-3 py-16 text-center">
        <div class="rounded-full bg-surface-gray-2 p-4">
          <FeatherIcon name="database" class="h-6 w-6 text-ink-gray-5" />
        </div>
        <p class="text-base text-ink-gray-7">
          {{ isSuperAdmin ? 'No buckets yet' : 'No buckets shared with you' }}
        </p>
        <p class="text-p-sm text-ink-gray-5">
          {{ isSuperAdmin
            ? 'Create your first bucket to start storing objects.'
            : 'An administrator can grant you access to a bucket.' }}
        </p>
        <Button
          v-if="isSuperAdmin"
          variant="solid"
          theme="gray"
          icon-left="plus"
          label="Create Bucket"
          class="mt-2"
          @click="showCreate = true"
        />
      </div>
    </div>

    <!-- Create Bucket Dialog -->
    <Dialog
      v-model="showCreate"
      :key="'create-bucket-' + showCreate"
      :options="{
        title: 'Create Bucket',
        icon: { name: 'database' },
        size: 'lg',
      }"
    >
      <template #body-content>
        <div class="space-y-4" @pointerdown.stop>
          <FormControl
            v-model="bucketName"
            label="Bucket Name"
            type="text"
            placeholder="my-bucket-name"
            required
            description="Must be globally unique, lowercase, 3–63 characters."
          />
          <FormControl
            v-model="bucketEncryption"
            label="Encryption"
            type="select"
            :options="encryptionOptions"
          />
          <FormControl
            v-model="bucketAccessPolicy"
            label="Access Policy"
            type="select"
            :options="accessOptions"
          />
          <FormControl
            v-model="bucketVersioning"
            type="checkbox"
            label="Enable Versioning"
          />
        </div>
      </template>

      <template #actions="{ close }">
        <Button label="Cancel" @click="close" />
        <Button
          variant="solid"
          theme="gray"
          label="Create Bucket"
          :loading="creating"
          :disabled="!bucketName.trim()"
          @click="handleCreate(close)"
        />
      </template>
    </Dialog>

    <!-- Edit Bucket Dialog -->
    <Dialog
      v-model="showEdit"
      :key="'edit-bucket-' + showEdit"
      :options="{
        title: 'Bucket Settings',
        icon: { name: 'settings' },
        size: 'lg',
      }"
    >
      <template #body-content>
        <div class="space-y-4" @pointerdown.stop>
          <FormControl
            :modelValue="editBucketRef?.name"
            label="Bucket Name"
            type="text"
            disabled
            description="Bucket name cannot be modified after creation."
          />
          <FormControl
            v-model="editEncryption"
            label="Encryption"
            type="select"
            :options="encryptionOptions"
          />
          <FormControl
            v-model="editAccessPolicy"
            label="Access Policy"
            type="select"
            :options="accessOptions"
          />
          <FormControl
            v-model="editVersioning"
            type="checkbox"
            label="Enable Versioning"
          />
        </div>
      </template>

      <template #actions="{ close }">
        <Button label="Cancel" @click="close" />
        <Button
          variant="solid"
          theme="gray"
          label="Save Changes"
          :loading="updating"
          @click="handleUpdate(close)"
        />
      </template>
    </Dialog>

    <!-- Manage Bucket Access / ACL Dialog -->
    <Dialog
      v-model="showAcl"
      :key="'acl-bucket-' + showAcl"
      :options="{
        title: `Bucket Access: ${selectedBucket?.name}`,
        icon: { name: 'shield' },
        size: 'xl',
      }"
    >
      <template #body-content>
        <div class="space-y-4" @pointerdown.stop>
          <div class="flex items-center justify-between">
            <p class="text-xs text-ink-gray-5">
              Control which users and groups have permissions on this bucket. Superusers and the bucket owner always have full access.
            </p>
            <Button
              v-if="!showGrantForm"
              variant="solid"
              theme="gray"
              size="sm"
              icon-left="plus"
              label="Grant Access"
              @click="openGrantAccess"
            />
          </div>

          <!-- Grant Access Inline Form -->
          <div v-if="showGrantForm" class="rounded-md border border-outline-gray-1 bg-surface-gray-1 p-4 space-y-3">
            <div class="flex items-center justify-between">
              <h4 class="text-sm font-medium text-ink-gray-9">Grant New Permission</h4>
              <Button variant="ghost" size="sm" icon="x" @click="showGrantForm = false" />
            </div>

            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="block text-xs font-medium text-ink-gray-5 mb-1">Grantee Type</label>
                <div class="flex items-center gap-4 py-1">
                  <label class="flex items-center gap-1.5 text-sm text-ink-gray-8 cursor-pointer">
                    <input type="radio" value="user" v-model="grantType" class="text-gray-900" />
                    <span>User</span>
                  </label>
                  <label class="flex items-center gap-1.5 text-sm text-ink-gray-8 cursor-pointer">
                    <input type="radio" value="group" v-model="grantType" class="text-gray-900" />
                    <span>Group</span>
                  </label>
                </div>
              </div>

              <div>
                <label class="block text-xs font-medium text-ink-gray-5 mb-1">{{ grantType === 'user' ? 'Select User' : 'Select Group' }}</label>
                <select
                  v-if="grantType === 'user'"
                  v-model="grantUserId"
                  class="w-full rounded-md border border-outline-gray-1 bg-surface-white px-3 py-1.5 text-sm text-ink-gray-9 focus:border-gray-900 focus:outline-none"
                >
                  <option v-for="u in allUsers" :key="u.id" :value="String(u.id)">
                    {{ u.username }} ({{ u.email || 'no email' }})
                  </option>
                </select>
                <select
                  v-else
                  v-model="grantGroupId"
                  class="w-full rounded-md border border-outline-gray-1 bg-surface-white px-3 py-1.5 text-sm text-ink-gray-9 focus:border-gray-900 focus:outline-none"
                >
                  <option v-for="g in allGroups" :key="g.id" :value="String(g.id)">
                    {{ g.name }} ({{ g.user_count }} members)
                  </option>
                </select>
              </div>
            </div>

            <div>
              <label class="block text-xs font-medium text-ink-gray-5 mb-1.5">Permissions</label>
              <div class="flex flex-wrap gap-4">
                <label v-for="p in ['read', 'write', 'delete', 'list', 'admin']" :key="p" class="flex items-center gap-1.5 text-sm text-ink-gray-8 cursor-pointer">
                  <input type="checkbox" :value="p" v-model="grantPerms" class="rounded text-gray-900" />
                  <span class="capitalize">{{ p }}</span>
                </label>
              </div>
            </div>

            <div class="flex justify-end gap-2 pt-2">
              <Button size="sm" label="Cancel" @click="showGrantForm = false" />
              <Button size="sm" variant="solid" theme="gray" label="Grant" :loading="grantingAcl" @click="handleGrantAccess" />
            </div>
          </div>

          <!-- ACL Entries Table -->
          <div v-if="aclsLoading" class="py-8 text-center text-sm text-ink-gray-5">Loading access rules...</div>
          <div v-else-if="bucketAcls.length" class="divide-y divide-outline-gray-1 rounded-md border border-outline-gray-1 overflow-hidden">
            <div
              v-for="acl in bucketAcls"
              :key="acl.id"
              class="flex items-center justify-between bg-surface-white px-4 py-3"
            >
              <div>
                <div class="flex items-center gap-2">
                  <FeatherIcon :name="acl.username ? 'user' : 'users'" class="h-4 w-4 text-ink-gray-5" />
                  <p class="text-sm font-medium text-ink-gray-9">
                    {{ acl.username || acl.group_name }}
                  </p>
                  <Badge :label="acl.username ? 'User' : 'Group'" theme="gray" variant="subtle" size="sm" />
                </div>
                <div class="flex items-center gap-1.5 mt-1">
                  <Badge
                    v-for="perm in acl.permissions"
                    :key="perm"
                    :label="perm"
                    :theme="perm === 'admin' ? 'red' : perm === 'write' ? 'blue' : 'gray'"
                    variant="subtle"
                    size="sm"
                  />
                </div>
              </div>
              <Button icon="trash-2" variant="ghost" theme="red" size="sm" title="Revoke permissions" @click="promptRevokeAcl(acl)" />
            </div>
          </div>
          <div v-else class="rounded-md border border-dashed border-outline-gray-1 py-8 text-center text-p-sm text-ink-gray-5">
            No custom permissions granted on this bucket. Only the bucket owner and superusers have access.
          </div>
        </div>
      </template>

      <template #actions="{ close }">
        <Button variant="solid" theme="gray" label="Done" @click="close" />
      </template>
    </Dialog>
  </div>
</template>
