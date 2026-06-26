<script setup>
import { ref } from 'vue'
import { Dialog, Button, FormControl, toast } from 'frappe-ui'
import { activeBucket } from '../stores/buckets'
import { useFilesSingleton } from '../data/files'

const open = defineModel({ type: Boolean, default: false })

const { currentPrefix, refreshList } = useFilesSingleton()
const folderName = ref('')
const creating = ref(false)

async function handleCreate() {
  if (!folderName.value.trim()) return
  creating.value = true
  try {
    const uuid = activeBucket.value?.uuid
    if (!uuid) throw new Error('No bucket selected')
    const token = localStorage.getItem('p2_token') || ''

    const resp = await fetch(`/api/v1/core/volume/${uuid}/folder/`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        prefix: currentPrefix.value || '',
        folder_name: folderName.value.trim(),
      }),
    })
    const data = await resp.json()
    if (!resp.ok || data.error) {
      throw new Error(data.error || data.detail || `HTTP ${resp.status}`)
    }
    toast.success(`Created folder "${folderName.value}"`)
    open.value = false
    folderName.value = ''
    refreshList()
  } catch (e) {
    toast.error(e.message || 'Failed to create folder')
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <Dialog
    v-model="open"
    :key="'create-folder-' + open"
    :options="{
      title: 'Create Folder',
      icon: { name: 'folder-plus' },
    }"
  >
    <template #body-content>
      <div class="space-y-4" @pointerdown.stop>
        <FormControl
          v-model="folderName"
          label="Folder Name"
          type="text"
          placeholder="e.g. my-folder"
          required
          description="Creates a folder prefix in the bucket."
        />
      </div>
    </template>

    <template #actions="{ close }">
      <div class="flex justify-end gap-2 w-full">
        <Button label="Cancel" @click="close" />
        <Button
          variant="solid"
          theme="gray"
          label="Create"
          :loading="creating"
          :disabled="!folderName.trim()"
          @click="handleCreate"
        />
      </div>
    </template>
  </Dialog>
</template>
