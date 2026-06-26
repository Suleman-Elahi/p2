<script setup>
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Badge, Dialog, FormControl, toast, FeatherIcon, confirmDialog } from 'frappe-ui'
import { useBucketsSingleton } from '../stores/buckets'

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
</script>

<template>
  <div class="flex h-full flex-col">
    <!-- Header -->
    <header class="sticky top-0 z-10 flex min-h-12 items-center justify-between border-b border-outline-gray-1 bg-surface-white px-3 sm:px-5">
      <h1 class="text-2xl font-semibold text-ink-gray-9">Buckets</h1>
      <Button variant="solid" theme="gray" icon-left="database" label="Create Bucket" @click="showCreate = true" />
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
            <Button icon="settings" variant="ghost" theme="gray" size="sm" @click.stop="openEditBucket(b)" />
            <Button icon="trash-2" variant="ghost" theme="red" size="sm" @click.stop="promptDeleteBucket(b)" />
            <Button icon="chevron-right" variant="ghost" size="sm" @click="openBucket(b)" />
          </div>
        </div>
      </div>

      <!-- Empty state -->
      <div v-else class="flex flex-col items-center justify-center gap-3 py-16 text-center">
        <div class="rounded-full bg-surface-gray-2 p-4">
          <FeatherIcon name="database" class="h-6 w-6 text-ink-gray-5" />
        </div>
        <p class="text-base text-ink-gray-7">No buckets yet</p>
        <p class="text-p-sm text-ink-gray-5">Create your first bucket to start storing objects.</p>
        <Button variant="solid" theme="gray" icon-left="plus" label="Create Bucket" class="mt-2" @click="showCreate = true" />
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
  </div>
</template>
