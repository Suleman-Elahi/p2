<script setup>
import { ref } from 'vue'
import { Dialog, Button, toast, FeatherIcon } from 'frappe-ui'
import { useFilesSingleton, uploadFiles } from '../data/files'
import { activeBucket } from '../stores/buckets'

const open = defineModel({ type: Boolean, default: false })

const { currentPrefix, refreshList } = useFilesSingleton()
const selectedFiles = ref([])
const uploading = ref(false)
const progressText = ref('')
const fileInput = ref(null)
const folderInput = ref(null)

function onFileChange(e) {
  selectedFiles.value = Array.from(e.target.files || [])
}

function onFolderChange(e) {
  selectedFiles.value = Array.from(e.target.files || [])
}

function onDrop(e) {
  if (uploading.value) return
  selectedFiles.value = Array.from(e.dataTransfer?.files || [])
}

function onDragOver(e) {
  e.preventDefault()
}

async function handleUpload(close) {
  if (!selectedFiles.value.length) return
  uploading.value = true
  progressText.value = `Preparing upload of ${selectedFiles.value.length} files...`
  try {
    const uuid = activeBucket.value?.uuid
    const prefix = currentPrefix.value || ''
    await uploadFiles(uuid, prefix, selectedFiles.value, (current, total, filename) => {
      progressText.value = `Uploading: ${current}/${total} - ${filename}`
    })
    toast.success(`Uploaded ${selectedFiles.value.length} file(s)`)
    close()
    selectedFiles.value = []
    refreshList()
  } catch (e) {
    toast.error(e.message)
  } finally {
    uploading.value = false
    progressText.value = ''
  }
}

function clearFiles() {
  selectedFiles.value = []
  if (fileInput.value) fileInput.value.value = ''
  if (folderInput.value) folderInput.value.value = ''
}
</script>

<template>
  <Dialog
    v-model="open"
    :key="'upload-' + open"
    :options="{ title: 'Upload Files', icon: { name: 'upload' }, size: 'lg' }"
  >
    <template #body-content>
      <div class="space-y-4" @pointerdown.stop>
        <!-- Drop zone -->
        <div
          class="flex flex-col items-center justify-center gap-2 rounded-md border-2 border-dashed border-outline-gray-2 bg-surface-gray-1 px-4 py-8 text-center transition-colors"
          :class="uploading ? 'cursor-not-allowed opacity-60' : 'cursor-pointer hover:border-outline-gray-3'"
          @drop.prevent="onDrop"
          @dragover="onDragOver"
          @click="!uploading && fileInput?.click()"
        >
          <FeatherIcon name="upload-cloud" class="h-8 w-8 text-ink-gray-4" />
          <p class="text-sm text-ink-gray-7">Drag and drop files here</p>
          <p class="text-p-xs text-ink-gray-5">or click to browse files</p>
          <input
            ref="fileInput"
            type="file"
            multiple
            :disabled="uploading"
            class="hidden"
            @change="onFileChange"
          />
        </div>

        <!-- Folder upload -->
        <div class="flex items-center justify-between rounded-md border border-outline-gray-1 bg-surface-white px-4 py-3">
          <div>
            <p class="text-sm text-ink-gray-7">Upload entire folder</p>
            <p class="text-p-xs text-ink-gray-5">Preserves folder structure inside the bucket</p>
          </div>
          <Button
            variant="subtle"
            theme="gray"
            icon-left="folder-plus"
            label="Choose Folder"
            :disabled="uploading"
            @click="folderInput?.click()"
          />
          <input
            ref="folderInput"
            type="file"
            webkitdirectory
            multiple
            :disabled="uploading"
            class="hidden"
            @change="onFolderChange"
          />
        </div>

        <!-- Selected files list -->
        <div v-if="selectedFiles.length" class="text-sm text-ink-gray-7">
          <div class="flex items-center justify-between mb-2">
            <p><strong>{{ selectedFiles.length }}</strong> file(s) selected</p>
            <Button icon="x" variant="ghost" size="sm" :disabled="uploading" @click="clearFiles" />
          </div>
          <div v-if="uploading && progressText" class="mb-2 p-2.5 bg-blue-50 border border-blue-200 rounded text-xs text-blue-700 font-medium animate-pulse truncate">
            {{ progressText }}
          </div>
          <ul class="space-y-1 max-h-32 overflow-auto rounded border border-outline-gray-1 bg-surface-gray-1 p-2">
            <li v-for="(f, i) in selectedFiles" :key="i" class="text-xs text-ink-gray-5 truncate">
              {{ f.webkitRelativePath || f.name }} ({{ (f.size / 1024).toFixed(1) }} KB)
            </li>
          </ul>
        </div>
      </div>
    </template>

    <template #actions="{ close }">
      <div class="flex justify-end gap-2 w-full">
        <Button label="Cancel" :disabled="uploading" @click="close" />
        <Button
          variant="solid" theme="gray" label="Upload"
          :loading="uploading" :disabled="!selectedFiles.length"
          @click="handleUpload(close)"
        />
      </div>
    </template>
  </Dialog>
</template>
