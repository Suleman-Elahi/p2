<script setup>
import { ref } from 'vue'
import { Button, Badge, Tooltip, toast, FeatherIcon, confirmDialog, Dialog, FormControl } from 'frappe-ui'
import {
  formatBytes,
  formatDate,
  storageClassTheme,
  isPreviewable,
  downloadFile,
} from '../data/files'
import { activeBucket } from '../stores/buckets'
import { useFilesSingleton } from '../data/files'

const props = defineProps({
  object: { type: Object, required: true },
})

const emit = defineEmits(['close', 'preview', 'deleted'])

const { deleteObject } = useFilesSingleton()

const showPresignDialog = ref(false)
const expiresIn = ref(3600)
const generatingPresignedUrl = ref(false)

async function generateAndCopyPresignedUrl(close) {
  generatingPresignedUrl.value = true
  try {
    const token = localStorage.getItem('p2_token') || ''
    const resp = await fetch('/api/v1/s3/presign/', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`
      },
      body: JSON.stringify({
        bucket: activeBucket.value?.name,
        key: props.object.key,
        method: 'GET',
        expires_in: expiresIn.value,
        base_url: window.location.origin
      })
    })
    const data = await resp.json()
    if (!resp.ok) throw new Error(data.error || 'Failed to generate URL')
    
    await navigator.clipboard.writeText(data.url)
    toast.success('Presigned URL copied to clipboard')
    close()
  } catch (e) {
    toast.error(e.message)
  } finally {
    generatingPresignedUrl.value = false
  }
}

async function downloadObject() {
  const uuid = activeBucket.value?.uuid
  if (!uuid) return
  try {
    await downloadFile(uuid, props.object.key, props.object.name)
    toast.success('Download started')
  } catch (e) {
    toast.error(e.message || 'Download failed')
  }
}

function promptDelete() {
  confirmDialog({
    title: 'Delete Object?',
    message: `Are you sure you want to delete "${props.object.name}"? This action cannot be undone.`,
    theme: 'red',
    confirmLabel: 'Delete',
    onConfirm: async ({ hideDialog }) => {
      const uuid = activeBucket.value?.uuid
      if (!uuid) return
      await deleteObject(uuid, props.object.key)
      toast.success(`Deleted "${props.object.name}"`)
      emit('deleted')
      emit('close')
      hideDialog()
    },
  })
}

function featherIcon(contentType) {
  if (!contentType) return 'file'
  if (contentType.startsWith('image/')) return 'image'
  if (contentType.startsWith('video/')) return 'video'
  if (contentType.startsWith('audio/')) return 'music'
  if (contentType === 'application/pdf') return 'file-text'
  if (contentType === 'application/json') return 'code'
  if (contentType === 'text/html' || contentType === 'text/css' || contentType === 'application/javascript') return 'code'
  if (contentType === 'text/csv') return 'grid'
  if (contentType === 'application/gzip') return 'archive'
  return 'file'
}
</script>

<template>
  <aside class="w-72 shrink-0 overflow-y-auto border-l border-outline-gray-1 bg-surface-white">
    <div class="px-4 py-4">
      <!-- Header -->
      <div class="flex items-start justify-between gap-2">
        <div class="flex items-center gap-2 min-w-0">
          <FeatherIcon :name="featherIcon(object.contentType)" class="h-4 w-4 text-ink-gray-5 shrink-0" />
          <h2 class="text-sm font-medium text-ink-gray-9 truncate">{{ object.name }}</h2>
        </div>
        <Button icon="x" variant="ghost" size="sm" @click="emit('close')" />
      </div>

      <!-- Actions -->
      <div class="mt-4 flex gap-2">
        <Tooltip v-if="isPreviewable(object.contentType)" text="Preview">
          <Button icon="eye" @click="emit('preview')" />
        </Tooltip>
        <Tooltip text="Download">
          <Button icon="download" @click="downloadObject" />
        </Tooltip>
        <Tooltip text="Presigned URL">
          <Button icon="link" @click="showPresignDialog = true" />
        </Tooltip>
        <Tooltip text="Delete">
          <Button icon="trash-2" theme="red" @click="promptDelete" />
        </Tooltip>
      </div>

      <!-- Metadata -->
      <div class="mt-5 space-y-4">
        <div>
          <p class="text-xs text-ink-gray-5 mb-1">Object Key</p>
          <p class="text-xs text-ink-gray-8 break-all font-mono bg-surface-gray-1 rounded px-2 py-1.5">{{ object.key }}</p>
        </div>

        <div class="grid grid-cols-2 gap-3">
          <div>
            <p class="text-xs text-ink-gray-5 mb-1">Size</p>
            <p class="text-sm text-ink-gray-8">{{ formatBytes(object.size) }}</p>
          </div>
          <div>
            <p class="text-xs text-ink-gray-5 mb-1">Storage Class</p>
            <Badge :label="object.storageClass" :theme="storageClassTheme(object.storageClass)" variant="subtle" size="sm" />
          </div>
        </div>

        <div>
          <p class="text-xs text-ink-gray-5 mb-1">Content Type</p>
          <p class="text-sm text-ink-gray-8">{{ object.contentType }}</p>
        </div>

        <div>
          <p class="text-xs text-ink-gray-5 mb-1">Last Modified</p>
          <p class="text-sm text-ink-gray-8">{{ formatDate(object.lastModified) }}</p>
        </div>

        <div>
          <p class="text-xs text-ink-gray-5 mb-1">S3 URI</p>
          <p class="text-xs text-ink-gray-6 break-all font-mono bg-surface-gray-1 rounded px-2 py-1.5">
            s3://{{ activeBucket?.name || 'bucket' }}/{{ object.key }}
          </p>
        </div>

        <div>
          <p class="text-xs text-ink-gray-5 mb-1">Preview</p>
          <p class="text-sm" :class="isPreviewable(object.contentType) ? 'text-ink-green-6' : 'text-ink-gray-5'">
            {{ isPreviewable(object.contentType) ? 'Supported' : 'Not available' }}
          </p>
        </div>
      </div>
    </div>
  </aside>

  <Dialog v-model="showPresignDialog" :options="{ title: 'Presigned URL', size: 'sm' }">
    <template #body-content>
      <FormControl
        v-model="expiresIn"
        label="Expires In (seconds)"
        type="number"
        placeholder="3600"
      />
    </template>
    <template #actions="{ close }">
      <div class="flex justify-end gap-2 w-full">
        <Button label="Cancel" @click="close" />
        <Button
          variant="solid" theme="gray" label="Copy URL"
          :loading="generatingPresignedUrl"
          @click="generateAndCopyPresignedUrl(close)"
        />
      </div>
    </template>
  </Dialog>
</template>
