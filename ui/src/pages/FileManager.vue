<script setup>
import { ref, watch, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button, Badge, Dropdown, Tooltip, FeatherIcon, Breadcrumbs, toast, confirmDialog } from 'frappe-ui'
import {
  useFilesSingleton,
  formatBytes,
  formatDate,
  isPreviewable,
  storageClassTheme,
  fileIconName,
  downloadFolder,
  downloadFile,
} from '../data/files'
import { activeBucket, useBucketsSingleton } from '../stores/buckets'
import UploadDialog from '../components/UploadDialog.vue'
import CreateFolderDialog from '../components/CreateFolderDialog.vue'
import ObjectDetails from '../components/ObjectDetails.vue'
import FilePreview from '../components/FilePreview.vue'

const route = useRoute()
const router = useRouter()
const { buckets, selectBucket } = useBucketsSingleton()
const {
  objects, folders, currentPrefix, loading,
  breadcrumbs, fetchObjects, loadMore, navigateTo, navigateUp, resetNavigation,
  deleteObject, deleteFolder, hasMore, searchQuery, setSearchQuery, refreshList,
} = useFilesSingleton()

const showUpload = ref(false)
const showCreateFolder = ref(false)
const selectedObject = ref(null)
const showPreview = ref(false)
const viewMode = ref('list')

const selectedItems = ref(new Set())
const bulkActionLoading = ref(false)
const searchText = ref('')
const selectAllMode = ref(false)  // true = "all items in current folder" selected

// Debounced search: update searchQuery which triggers refetch via listUrl
let searchTimer = null
function onSearchInput(val) {
  searchText.value = val
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    setSearchQuery(val)
  }, 300)
}

function triggerSearch() {
  clearTimeout(searchTimer)
  setSearchQuery(searchText.value)
}

function clearSearch() {
  searchText.value = ''
  setSearchQuery('')
}

function toggleSelection(key) {
  const newSet = new Set(selectedItems.value)
  if (newSet.has(key)) {
    newSet.delete(key)
  } else {
    newSet.add(key)
  }
  selectedItems.value = newSet
  selectAllMode.value = false
}

// Total visible count
const visibleCount = computed(() => objects.value.length + folders.value.length)
const allVisibleSelected = computed(() =>
  visibleCount.value > 0 && selectedItems.value.size >= visibleCount.value
)

function selectAllVisible() {
  const newSet = new Set(selectedItems.value)
  for (const obj of objects.value) {
    newSet.add(obj.key)
  }
  for (const folder of folders.value) {
    newSet.add(folder.prefix)
  }
  selectedItems.value = newSet
}

function selectAllInFolder() {
  selectAllMode.value = true
  // Set selected items to all visible + flag that we mean "all"
  selectAllVisible()
}

function clearSelection() {
  selectedItems.value = new Set()
  selectAllMode.value = false
}

watch(currentPrefix, clearSelection)

async function handleBulkDelete() {
  const isAllMode = selectAllMode.value
  const keys = [...selectedItems.value]
  const count = isAllMode ? 'all' : keys.length

  confirmDialog({
    title: 'Delete Files?',
    message: isAllMode
      ? `Are you sure you want to delete ALL files in this folder? This cannot be undone.`
      : `Are you sure you want to delete ${count} file${count !== 1 ? 's' : ''}? This action cannot be undone.`,
    theme: 'red',
    confirmLabel: 'Delete',
    onConfirm: async ({ hideDialog }) => {
      hideDialog()
      if (!activeBucket.value?.uuid) return
      bulkActionLoading.value = true
      clearSelection()

      if (isAllMode) {
        // Delete entire folder contents in one call
        try {
          await deleteFolder(activeBucket.value.uuid, currentPrefix.value)
          toast.success('Deleted all files in folder')
        } catch (e) {
          toast.error(e.message || 'Delete failed')
        }
      } else {
        let deleted = 0
        let failed = 0
        for (const key of keys) {
          try {
            await deleteObject(activeBucket.value.uuid, key)
            deleted++
          } catch {
            failed++
          }
        }
        if (failed) {
          toast.error(`Deleted ${deleted}, ${failed} failed`)
        } else {
          toast.success(`Deleted ${deleted} file${deleted !== 1 ? 's' : ''}`)
        }
      }
      refreshList()
      bulkActionLoading.value = false
    },
  })
}

async function handleBulkDownload() {
  if (selectedItems.value.size === 0) return
  if (!activeBucket.value?.uuid) return
  bulkActionLoading.value = true
  const keys = [...selectedItems.value]
  clearSelection()
  try {
    for (const key of keys) {
      await downloadFile(activeBucket.value.uuid, key)
    }
    toast.success(`Started download for ${keys.length} files`)
  } catch (e) {
    toast.error('Failed to start downloads')
  } finally {
    bulkActionLoading.value = false
  }
}

function promptDeleteFile(file) {
  confirmDialog({
    title: 'Delete File?',
    message: `Are you sure you want to delete "${file.name}"? This action cannot be undone.`,
    theme: 'red',
    confirmLabel: 'Delete',
    onConfirm: async ({ hideDialog }) => {
      hideDialog()
      if (!activeBucket.value?.uuid) return
      try {
        await deleteObject(activeBucket.value.uuid, file.key)
        toast.success(`Deleted "${file.name}"`)
      } catch (e) {
        toast.error(e.message || 'Delete failed')
      }
      if (selectedObject.value?.key === file.key) closeDetails()
      refreshList()
    },
  })
}

function promptDeleteFolder(folder) {
  confirmDialog({
    title: 'Delete Folder?',
    message: `Are you sure you want to delete "${folder.name}" and all its contents? This action cannot be undone.`,
    theme: 'red',
    confirmLabel: 'Delete',
    onConfirm: async ({ hideDialog }) => {
      hideDialog()
      if (!activeBucket.value?.uuid) return
      try {
        await deleteFolder(activeBucket.value.uuid, folder.prefix)
        toast.success(`Deleted "${folder.name}"`)
      } catch (e) {
        toast.error(e.message || 'Delete failed')
      }
      refreshList()
    },
  })
}

watch(
  [() => route.params.name, () => buckets.value.length],
  async ([name]) => {
    if (name) {
      const bucket = buckets.value.find(b => b.name === name)
      if (bucket) {
        selectBucket(bucket)
        resetNavigation()
        selectedObject.value = null
        showPreview.value = false
        clearSelection()
        await fetchObjects()
      }
    }
  },
  { immediate: true },
)

const isAtRoot = computed(() => currentPrefix.value === '')

function handleFileClick(file) {
  selectedObject.value = file
  showPreview.value = false
}

function handlePreview(file) {
  selectedObject.value = file
  showPreview.value = true
}

function closeDetails() {
  selectedObject.value = null
  showPreview.value = false
}

const newActions = [
  { label: 'Upload File', icon: 'upload', onClick: () => (showUpload.value = true) },
  { label: 'Create Folder', icon: 'folder-plus', onClick: () => (showCreateFolder.value = true) },
]

</script>

<template>
  <div class="flex h-full flex-col bg-surface-white text-ink-gray-9">
    <!-- Header -->
    <header class="sticky top-0 z-10 flex min-h-12 items-center justify-between border-b border-outline-gray-1 bg-surface-white px-3 sm:px-5">
      <div class="flex items-center gap-2 min-w-0">
        <button
          class="flex items-center justify-center h-7 w-7 rounded text-ink-gray-5 hover:text-ink-gray-8 hover:bg-surface-gray-2 transition-colors"
          @click="router.push('/buckets')"
        >
          <FeatherIcon name="arrow-left" class="h-4 w-4" />
        </button>
        <!-- Breadcrumbs -->
        <Breadcrumbs :items="breadcrumbs.map(c => ({ label: c.label, onClick: () => navigateTo(c.prefix) }))" />
      </div>
      <div class="flex items-center gap-2">
        <!-- Search box -->
        <div class="relative">
          <FeatherIcon name="search" class="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-ink-gray-4" />
          <input
            :value="searchText"
            type="text"
            placeholder="Search files..."
            class="h-7 w-48 rounded border border-outline-gray-1 bg-surface-white pl-8 pr-7 text-sm text-ink-gray-9 placeholder-ink-gray-4 focus:border-outline-gray-3 focus:outline-none focus:ring-0"
            @input="onSearchInput($event.target.value)"
            @keydown.enter.prevent="triggerSearch()"
          />
          <button
            v-if="searchText"
            class="absolute right-1.5 top-1/2 -translate-y-1/2 h-4 w-4 flex items-center justify-center rounded text-ink-gray-4 hover:text-ink-gray-7"
            @click="clearSearch()"
          >
            <FeatherIcon name="x" class="h-3 w-3" />
          </button>
        </div>
        <template v-if="selectedItems.size > 0">
          <span class="text-sm font-medium text-ink-gray-7">
            {{ selectAllMode ? 'All items' : `${selectedItems.size} selected` }}
          </span>
          <Button v-if="!allVisibleSelected" label="Select All" variant="ghost" size="sm" @click="selectAllVisible" />
          <Button v-else-if="hasMore && !selectAllMode" label="Select all in folder" variant="ghost" size="sm" @click="selectAllInFolder" />
          <Button icon-left="download" label="Download" variant="subtle" theme="gray" @click="handleBulkDownload" :loading="bulkActionLoading" />
          <Button icon-left="trash-2" theme="red" label="Delete" variant="solid" @click="handleBulkDelete" :loading="bulkActionLoading" />
          <Button icon="x" variant="ghost" @click="clearSelection" />
        </template>
        <template v-else>
          <Tooltip text="Grid view">
            <Button icon="grid" :variant="viewMode === 'grid' ? 'subtle' : 'ghost'" @click="viewMode = 'grid'" />
          </Tooltip>
          <Tooltip text="List view">
            <Button icon="list" :variant="viewMode === 'list' ? 'subtle' : 'ghost'" @click="viewMode = 'list'" />
          </Tooltip>
          <Dropdown :options="newActions">
            <Button variant="solid" theme="gray" icon-left="plus" label="New" />
          </Dropdown>
        </template>
      </div>
    </header>

    <!-- Content -->
    <div class="flex flex-1 overflow-hidden">
      <!-- File listing -->
      <div class="flex-1 overflow-y-auto">
        <div class="mx-auto w-full max-w-[940px] px-3 pt-5 pb-40 sm:px-5">
          <!-- Back button -->
          <button
            v-if="!isAtRoot"
            class="mb-3 flex items-center gap-2 text-sm text-ink-gray-6 hover:text-ink-gray-9 transition-colors"
            @click="navigateUp"
          >
            <FeatherIcon name="arrow-left" class="h-4 w-4" />
            Back
          </button>

          <!-- Loading -->
          <div v-if="loading" class="flex items-center justify-center py-16">
            <FeatherIcon name="loader" class="h-8 w-8 animate-spin text-ink-gray-3" />
          </div>

          <!-- List view -->
          <template v-else-if="viewMode === 'list'">
            <div class="mb-1 grid grid-cols-[40px_1fr_80px_110px_140px_80px] items-center gap-3 px-3 text-xs text-ink-gray-5">
              <span></span>
              <span>Name</span>
              <span>Size</span>
              <span>Storage</span>
              <span>Modified</span>
              <span>Actions</span>
            </div>

            <div class="divide-y divide-outline-gray-1 rounded-md border border-outline-gray-1 overflow-hidden">
              <!-- Folders -->
              <div
                v-for="folder in folders"
                :key="folder.prefix"
                class="grid w-full grid-cols-[40px_1fr_80px_110px_140px_80px] items-center gap-3 bg-surface-white px-3 py-2.5 hover:bg-surface-gray-1 transition-colors"
              >
                <span></span>
                <button class="flex items-center gap-2.5 text-left" @click="navigateTo(folder.prefix)">
                  <FeatherIcon name="folder" class="h-4 w-4 text-ink-gray-5 shrink-0" />
                  <span class="text-sm font-medium text-ink-gray-9 truncate">{{ folder.name }}/</span>
                </button>
                <span class="text-sm text-ink-gray-6">{{ formatBytes(folder.size) }}</span>
                <span class="text-sm text-ink-gray-5">—</span>
                <span class="text-xs text-ink-gray-5">{{ folder.objectCount }} items</span>
                <div class="flex justify-end gap-1">
                  <Tooltip text="Download ZIP">
                    <Button icon="download" variant="ghost" size="sm" @click.stop="downloadFolder(activeBucket.uuid, folder.prefix)" />
                  </Tooltip>
                  <Tooltip text="Delete folder">
                    <Button icon="trash-2" variant="ghost" theme="red" size="sm" @click.stop="promptDeleteFolder(folder)" />
                  </Tooltip>
                </div>
              </div>

              <!-- Files -->
              <div
                v-for="file in objects"
                :key="file.key"
                class="grid w-full grid-cols-[40px_1fr_80px_110px_140px_80px] items-center gap-3 bg-surface-white px-3 py-2.5 hover:bg-surface-gray-1 transition-colors cursor-pointer"
                :class="{ 'bg-surface-gray-2': selectedObject?.key === file.key || selectedItems.has(file.key) }"
                @click="handleFileClick(file)"
              >
                <div @click.stop class="flex items-center">
                  <input type="checkbox" :checked="selectedItems.has(file.key)" @change="toggleSelection(file.key)" class="h-4 w-4 rounded border-outline-gray-2 bg-surface-white text-ink-gray-9 focus:ring-0 cursor-pointer" />
                </div>
                <div class="flex items-center gap-2.5 min-w-0">
                  <FeatherIcon :name="fileIconName(file.contentType)" class="h-4 w-4 text-ink-gray-5 shrink-0" />
                  <span class="text-sm text-ink-gray-9 truncate">{{ file.name }}</span>
                </div>
                <span class="text-sm text-ink-gray-6">{{ formatBytes(file.size) }}</span>
                <div class="flex justify-center">
                  <Badge :label="file.storageClass" :theme="storageClassTheme(file.storageClass)" variant="subtle" size="sm" />
                </div>
                <span class="text-xs text-ink-gray-5">{{ formatDate(file.lastModified) }}</span>
                <div class="flex justify-end gap-1">
                  <Tooltip v-if="isPreviewable(file.contentType)" text="Preview">
                    <Button icon="eye" variant="ghost" size="sm" @click.stop="handlePreview(file)" />
                  </Tooltip>
                  <Tooltip text="Delete">
                    <Button icon="trash-2" variant="ghost" theme="red" size="sm" @click.stop="promptDeleteFile(file)" />
                  </Tooltip>
                </div>
              </div>
            </div>
          </template>

          <!-- Grid view -->
          <template v-else>
            <div v-if="folders.length" class="mb-5">
              <h3 class="mb-2 text-xs text-ink-gray-5">Folders</h3>
              <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                <div
                  v-for="folder in folders"
                  :key="folder.prefix"
                  class="flex items-center gap-3 rounded-md border border-outline-gray-1 bg-surface-white px-4 py-3 hover:bg-surface-gray-1 transition-colors group"
                >
                  <button class="flex flex-1 items-center gap-3 min-w-0 text-left" @click="navigateTo(folder.prefix)">
                    <FeatherIcon name="folder" class="h-5 w-5 text-ink-gray-5 shrink-0" />
                    <div class="min-w-0">
                      <p class="text-sm font-medium text-ink-gray-9 truncate">{{ folder.name }}/</p>
                      <p class="text-xs text-ink-gray-5">{{ folder.objectCount }} items • {{ formatBytes(folder.size) }}</p>
                    </div>
                  </button>
                  <div class="flex items-center gap-1">
                    <Tooltip text="Download ZIP">
                      <Button icon="download" variant="ghost" size="sm" @click.stop="downloadFolder(activeBucket.uuid, folder.prefix)" />
                    </Tooltip>
                    <Tooltip text="Delete folder">
                      <Button icon="trash-2" variant="ghost" theme="red" size="sm" @click.stop="promptDeleteFolder(folder)" />
                    </Tooltip>
                  </div>
                </div>
              </div>
            </div>

            <div v-if="objects.length">
              <h3 class="mb-2 text-xs text-ink-gray-5">Files</h3>
              <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                <div
                  v-for="file in objects"
                  :key="file.key"
                  class="flex flex-col items-center gap-2 rounded-md border border-outline-gray-1 bg-surface-white px-4 py-4 hover:bg-surface-gray-1 transition-colors group relative"
                  :class="{ 'bg-surface-gray-2 border-ink-gray-4': selectedObject?.key === file.key || selectedItems.has(file.key) }"
                >
                  <div class="absolute top-2 left-2 z-10" @click.stop>
                    <input type="checkbox" :checked="selectedItems.has(file.key)" @change="toggleSelection(file.key)" class="h-4 w-4 rounded border-outline-gray-2 bg-surface-white text-ink-gray-9 focus:ring-0 cursor-pointer opacity-0 group-hover:opacity-100 transition-opacity" :class="{ 'opacity-100': selectedItems.has(file.key) }" />
                  </div>
                  <button class="flex flex-col items-center gap-2 w-full mt-2" @click="handleFileClick(file)">
                    <FeatherIcon :name="fileIconName(file.contentType)" class="h-8 w-8 text-ink-gray-4" />
                    <p class="w-full text-sm text-ink-gray-9 truncate">{{ file.name }}</p>
                    <p class="text-xs text-ink-gray-5">{{ formatBytes(file.size) }}</p>
                  </button>
                  <div class="flex gap-1">
                    <Tooltip text="Delete">
                      <Button icon="trash-2" variant="ghost" theme="red" size="sm" @click.stop="promptDeleteFile(file)" />
                    </Tooltip>
                  </div>
                </div>
              </div>
            </div>
          </template>

          <!-- Load more -->
          <div v-if="hasMore" class="flex justify-center pt-4">
            <Button
              variant="subtle"
              theme="gray"
              label="Load more"
              :loading="loading"
              @click="loadMore"
            />
          </div>

          <!-- Empty state -->
          <div
            v-if="!loading && !folders.length && !objects.length"
            class="flex flex-col items-center justify-center gap-3 py-16 text-center"
          >
            <div class="rounded-full bg-surface-gray-2 p-4">
              <FeatherIcon name="folder" class="h-6 w-6 text-ink-gray-5" />
            </div>
            <p class="text-base text-ink-gray-7">This folder is empty</p>
            <p class="text-p-sm text-ink-gray-5">Upload files or create a subfolder to get started.</p>
            <Button variant="solid" theme="gray" icon-left="upload" label="Upload File" class="mt-2" @click="showUpload = true" />
          </div>
        </div>
      </div>

      <!-- Details / Preview panel -->
      <FilePreview v-if="showPreview && selectedObject" :object="selectedObject" @close="closeDetails" />
      <ObjectDetails v-else-if="selectedObject" :object="selectedObject" @close="closeDetails" @preview="showPreview = true" @deleted="fetchObjects()" />
    </div>

    <UploadDialog v-model="showUpload" />
    <CreateFolderDialog v-model="showCreateFolder" />
  </div>
</template>
