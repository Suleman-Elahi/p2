<script setup>
import { ref, computed, watch, onUnmounted } from 'vue'
import { Button, Badge, FeatherIcon } from 'frappe-ui'
import { getPreviewType, formatBytes } from '../data/files'
import { activeBucket } from '../stores/buckets'
import { getAccessToken } from '../stores/auth'

const props = defineProps({
  object: { type: Object, required: true },
})

const emit = defineEmits(['close'])

const previewType = computed(() => getPreviewType(props.object.contentType))

// ── Preview state ─────────────────────────────────────────────────────────

const previewUrl = ref(null)
const textContent = ref('')
const loading = ref(true)
const error = ref(null)

const apiPath = computed(() => {
  const uuid = activeBucket.value?.uuid
  if (!uuid) return null
  return `/api/v1/core/volume/${uuid}/blobs/download/?key=${encodeURIComponent(props.object.key)}`
})

async function loadPreview() {
  if (!apiPath.value) {
    loading.value = false
    error.value = 'No bucket selected'
    return
  }
  loading.value = true
  error.value = null
  textContent.value = ''
  if (previewUrl.value) {
    URL.revokeObjectURL(previewUrl.value)
    previewUrl.value = null
  }

  try {
    const res = await fetch(apiPath.value, {
      headers: { Authorization: `Bearer ${getAccessToken()}` },
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)

    if (previewType.value === 'code') {
      textContent.value = await res.text()
    } else {
      const blob = await res.blob()
      previewUrl.value = URL.createObjectURL(blob)
    }
  } catch (e) {
    error.value = e.message || 'Failed to load preview'
  } finally {
    loading.value = false
  }
}

watch(() => props.object?.key, loadPreview, { immediate: true })

onUnmounted(() => {
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
})

// ── Helpers ───────────────────────────────────────────────────────────────

function featherIcon(contentType) {
  if (!contentType) return 'file'
  if (contentType.startsWith('image/')) return 'image'
  if (contentType.startsWith('video/')) return 'video'
  if (contentType.startsWith('audio/')) return 'music'
  if (contentType === 'application/pdf') return 'file-text'
  if (contentType === 'application/json') return 'code'
  if (contentType === 'text/html' || contentType === 'text/css' || contentType === 'application/javascript') return 'code'
  if (contentType === 'text/csv') return 'grid'
  if (contentType === 'text/markdown') return 'file-text'
  return 'file'
}

function getLanguageLabel(contentType) {
  return {
    'application/json': 'JSON',
    'text/html': 'HTML',
    'text/css': 'CSS',
    'application/javascript': 'JavaScript',
    'text/markdown': 'Markdown',
    'text/csv': 'CSV',
    'text/plain': 'Plain Text',
  }[contentType] || 'Text'
}
</script>

<template>
  <aside class="flex w-[420px] shrink-0 flex-col border-l border-outline-gray-1 bg-surface-white">
    <!-- Preview header -->
    <div class="flex items-center justify-between border-b border-outline-gray-1 px-4 py-3">
      <div class="flex items-center gap-2 min-w-0">
        <FeatherIcon :name="featherIcon(object.contentType)" class="h-4 w-4 text-ink-gray-5 shrink-0" />
        <span class="text-sm font-medium text-ink-gray-9 truncate">{{ object.name }}</span>
        <Badge :label="formatBytes(object.size)" theme="gray" variant="subtle" size="sm" />
      </div>
      <Button icon="x" variant="ghost" size="sm" @click="emit('close')" />
    </div>

    <!-- Preview content -->
    <div class="flex-1 overflow-auto">
      <!-- Loading state -->
      <div v-if="loading" class="flex h-full min-h-48 items-center justify-center">
        <div class="text-center">
          <FeatherIcon name="loader" class="h-8 w-8 animate-spin text-ink-gray-3 mx-auto" />
          <p class="mt-3 text-p-sm text-ink-gray-5">Loading preview...</p>
        </div>
      </div>

      <!-- Error state -->
      <div v-else-if="error" class="flex h-full min-h-48 items-center justify-center p-4">
        <div class="text-center">
          <FeatherIcon name="alert-triangle" class="h-10 w-10 text-ink-gray-3 mx-auto" />
          <p class="mt-3 text-p-sm text-ink-gray-7">Preview failed</p>
          <p class="mt-1 text-p-xs text-ink-gray-5">{{ error }}</p>
        </div>
      </div>

      <!-- Image preview -->
      <template v-else-if="previewType === 'image' && previewUrl">
        <div class="flex h-full items-center justify-center bg-surface-gray-1 p-4">
          <img
            :src="previewUrl"
            :alt="object.name"
            class="max-h-full max-w-full rounded object-contain"
          />
        </div>
      </template>

      <!-- Video preview -->
      <template v-else-if="previewType === 'video' && previewUrl">
        <div class="flex h-full items-center justify-center bg-surface-gray-1 p-4">
          <video
            :src="previewUrl"
            controls
            class="max-h-full max-w-full rounded"
            style="max-height: 60vh;"
          >
            Your browser does not support the video element.
          </video>
        </div>
      </template>

      <!-- Audio preview -->
      <template v-else-if="previewType === 'audio' && previewUrl">
        <div class="flex h-full flex-col items-center justify-center p-6">
          <div class="mb-4 flex h-24 w-24 items-center justify-center rounded-full bg-surface-gray-1">
            <FeatherIcon name="music" class="h-12 w-12 text-ink-gray-4" />
          </div>
          <p class="mb-4 text-sm font-medium text-ink-gray-8 text-center">{{ object.name }}</p>
          <audio :src="previewUrl" controls class="w-full max-w-sm">
            Your browser does not support the audio element.
          </audio>
        </div>
      </template>

      <!-- PDF preview -->
      <template v-else-if="previewType === 'pdf' && previewUrl">
        <iframe
          :src="previewUrl"
          class="h-full w-full border-0"
          style="min-height: 60vh;"
          sandbox="allow-scripts allow-same-origin"
        />
      </template>

      <!-- Code / text preview -->
      <template v-else-if="previewType === 'code' && textContent">
        <div class="flex flex-col h-full">
          <div class="flex items-center justify-between border-b border-outline-gray-1 px-4 py-2">
            <Badge :label="getLanguageLabel(object.contentType)" theme="blue" variant="subtle" size="sm" />
            <span class="text-xs text-ink-gray-5">{{ formatBytes(object.size) }}</span>
          </div>
          <div class="flex-1 overflow-auto bg-surface-gray-1">
            <pre class="p-4 text-xs leading-relaxed text-ink-gray-8 font-mono whitespace-pre-wrap">{{ textContent }}</pre>
          </div>
        </div>
      </template>

      <!-- Unsupported -->
      <template v-else-if="!loading && !error">
        <div class="flex h-full min-h-48 items-center justify-center">
          <div class="text-center">
            <FeatherIcon name="file" class="h-12 w-12 text-ink-gray-3 mx-auto" />
            <p class="mt-3 text-p-sm text-ink-gray-7">Preview not available</p>
            <p class="text-p-xs text-ink-gray-5 mt-1">This file type cannot be previewed.</p>
          </div>
        </div>
      </template>
    </div>

    <!-- Footer -->
    <div class="border-t border-outline-gray-1 px-4 py-2.5">
      <p class="text-p-xs text-ink-gray-5">
        Supported: Images · Video · Audio · PDF · JSON · HTML · CSS · JS · Markdown · CSV · Text
      </p>
    </div>
  </aside>
</template>
