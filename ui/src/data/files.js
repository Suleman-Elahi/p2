import { ref, computed, watch } from 'vue'
import { useApi } from '../stores/api'
import { activeBucket } from '../stores/buckets'

// ── Shared navigation state ──────────────────────────────────────────────
const currentPrefix = ref('')
const searchQuery = ref('')
const refreshKey = ref(0)

function triggerRefresh() {
  refreshKey.value++
}

// ── Composable: file listing ─────────────────────────────────────────────

export function useFiles() {
  const nextStartAfter = ref('')
  const hasMore = ref(false)
  const allObjects = ref([])
  const allFolders = ref([])
  const isFetchingMore = ref(false)

  // Reactive URL — computed based on current pagination/filtering state
  const listUrl = computed(() => {
    const uuid = activeBucket.value?.uuid
    if (!uuid) return ''
    let url = `/core/volume/${uuid}/blobs/?max_keys=100&prefix=${encodeURIComponent(currentPrefix.value)}&_k=${refreshKey.value}`
    if (nextStartAfter.value) {
      url += `&start_after=${encodeURIComponent(nextStartAfter.value)}`
    }
    if (searchQuery.value) {
      url += `&search=${encodeURIComponent(searchQuery.value)}`
    }
    return url
  })

  const { data, isFetching, error, execute: refetch } = useApi(listUrl, {
    immediate: false,
  }).json()

  // Watch data updates to manage pagination state and append/replace files
  watch(data, (newData) => {
    if (!newData) return

    nextStartAfter.value = newData.next_start_after || ''
    hasMore.value = newData.has_more || false

    const mappedObjects = (newData.objects || []).map(obj => ({
      ...obj,
      name: obj.key ? obj.key.split('/').pop() : obj.key,
      contentType: obj.mime || 'application/octet-stream',
      storageClass: obj.storageClass || 'STANDARD',
      lastModified: obj.last_modified || obj.lastModified || '',
    }))

    const mappedFolders = (newData.folders || []).map(f => ({
      name: f.name || f.prefix.replace(currentPrefix.value, '').replace(/\/$/, ''),
      prefix: f.prefix,
      objectCount: f.object_count || 0,
      size: f.size || 0,
    }))

    if (isFetchingMore.value) {
      allObjects.value = [...allObjects.value, ...mappedObjects]
      // deduplicate folders by prefix
      const folderMap = new Map()
      for (const f of [...allFolders.value, ...mappedFolders]) {
        folderMap.set(f.prefix, f)
      }
      allFolders.value = Array.from(folderMap.values())
      isFetchingMore.value = false
    } else {
      allObjects.value = mappedObjects
      allFolders.value = mappedFolders
    }
  })

  async function fetchObjects() {
    nextStartAfter.value = ''
    isFetchingMore.value = false
    await refetch()
  }

  async function loadMore() {
    if (!hasMore.value || !nextStartAfter.value || isFetching.value) return
    isFetchingMore.value = true
    await refetch()
  }

  // Watch for changes that require resetting pagination and doing a fresh fetch
  watch(
    [() => activeBucket.value?.uuid, currentPrefix, searchQuery, refreshKey],
    () => {
      fetchObjects()
    }
  )

  // ── Delete object ────────────────────────────────────────────────────
  const delObjLoading = ref(false)
  const delObjError = ref(null)

  async function deleteObject(uuid, key) {
    delObjLoading.value = true
    delObjError.value = null
    try {
      const token = localStorage.getItem('p2_token') || ''
      const resp = await fetch(`/api/v1/core/volume/${uuid}/blobs/?key=${encodeURIComponent(key)}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}))
        throw new Error(body.detail || `HTTP ${resp.status}`)
      }
      // Caller decides when to refetch — don't auto-refresh here
    } catch (e) {
      delObjError.value = e
      throw e
    } finally {
      delObjLoading.value = false
    }
  }

  // ── Delete folder ────────────────────────────────────────────────────
  const delFolderLoading = ref(false)
  const delFolderError = ref(null)

  async function deleteFolder(uuid, prefix) {
    delFolderLoading.value = true
    delFolderError.value = null
    try {
      const token = localStorage.getItem('p2_token') || ''
      const resp = await fetch(`/api/v1/core/volume/${uuid}/folder/?prefix=${encodeURIComponent(prefix)}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}))
        throw new Error(body.detail || `HTTP ${resp.status}`)
      }
      // Caller decides when to refetch — don't auto-refresh here
    } catch (e) {
      delFolderError.value = e
      throw e
    } finally {
      delFolderLoading.value = false
    }
  }

  // downloadFile and uploadFiles are standalone exports — see below

  // ── Breadcrumb path segments ────────────────────────────────────────
  const breadcrumbs = computed(() => {
    const bucketName = activeBucket.value?.name || 'Bucket'
    const parts = currentPrefix.value.split('/').filter(Boolean)
    const crumbs = [{ label: bucketName, prefix: '' }]
    let accumulated = ''
    for (const part of parts) {
      accumulated += part + '/'
      crumbs.push({ label: part, prefix: accumulated })
    }
    return crumbs
  })

  function navigateTo(prefix) {
    currentPrefix.value = prefix
  }

  function navigateUp() {
    const parts = currentPrefix.value.split('/').filter(Boolean)
    parts.pop()
    const newPrefix = parts.length ? parts.join('/') + '/' : ''
    navigateTo(newPrefix)
  }

  function resetNavigation() {
    currentPrefix.value = ''
  }

  return {
    objects: allObjects,
    folders: allFolders,
    currentPrefix,
    loading: isFetching,
    error,
    breadcrumbs,
    fetchObjects,
    loadMore,
    navigateTo,
    navigateUp,
    resetNavigation,
    deleteObject,
    deleteFolder,
    hasMore,
    searchQuery,
    setSearchQuery: (q) => { searchQuery.value = q },
    refreshList: triggerRefresh,
    // downloadFile and uploadFiles are standalone exports
  }
}

// ── Singleton ────────────────────────────────────────────────────────────
let defaultInstance = null

export function useFilesSingleton() {
  if (!defaultInstance) defaultInstance = useFiles()
  return defaultInstance
}

export { currentPrefix, searchQuery }

// ── Standalone actions (don't need composable reactivity) ──────────────

export async function downloadFile(uuid, key, filename) {
  const resp = await fetch(`/api/v1/core/volume/${uuid}/blobs/download/?key=${encodeURIComponent(key)}&download=1`, {
    headers: { Authorization: `Bearer ${localStorage.getItem('p2_token') || ''}` },
  })
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  const blob = await resp.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename || key.split('/').pop() || 'download'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

export function downloadFolder(uuid, prefix) {
  const token = localStorage.getItem('p2_token') || ''
  const url = `/api/v1/core/volume/${uuid}/folder/download/?prefix=${encodeURIComponent(prefix || '')}`
  const a = document.createElement('a')
  a.href = url
  a.download = ''
  // Add auth via a temporary iframe/fetch approach — direct download with token
  fetch(url, { headers: { Authorization: `Bearer ${token}` } })
    .then(resp => {
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      return resp.blob()
    })
    .then(blob => {
      const blobUrl = URL.createObjectURL(blob)
      a.href = blobUrl
      const folderName = prefix ? prefix.split('/').filter(Boolean).pop() || 'folder' : 'bucket'
      a.download = `${folderName}.zip`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(blobUrl)
    })
    .catch(err => {
      import('frappe-ui').then(({ toast }) => toast.error(err.message || 'Download failed'))
    })
}

export async function uploadFiles(uuid, prefix, fileList, onProgress) {
  const total = fileList.length
  let uploadedCount = 0
  const results = []
  const concurrency = 6
  
  const fileArray = Array.from(fileList)
  
  async function worker() {
    while (fileArray.length > 0) {
      const file = fileArray.shift()
      if (!file) continue
      
      const form = new FormData()
      form.append('file', file, file.name)
      form.append('relativePath', file.webkitRelativePath || file.name)
      
      try {
        const resp = await fetch(
          `/api/v1/core/volume/${uuid}/upload/?prefix=${encodeURIComponent(prefix || '')}`,
          {
            method: 'POST',
            headers: { Authorization: `Bearer ${localStorage.getItem('p2_token') || ''}` },
            body: form,
          },
        )
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}))
          throw new Error(err.detail || `HTTP ${resp.status}`)
        }
        const data = await resp.json()
        results.push(data)
      } catch (err) {
        throw err
      } finally {
        uploadedCount++
        if (onProgress) {
          onProgress(uploadedCount, total, file.webkitRelativePath || file.name)
        }
      }
    }
  }
  
  const workers = []
  for (let i = 0; i < Math.min(concurrency, total); i++) {
    workers.push(worker())
  }
  
  await Promise.all(workers)
  return { uploaded: results.flatMap(r => r.uploaded || []) }
}

// ── Utility functions (no reactivity needed) ─────────────────────────────

export function formatBytes(bytes) {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}

export function formatDate(isoString) {
  return new Date(isoString).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function storageClassTheme(storageClass) {
  return { STANDARD: 'blue' }[storageClass] ?? 'gray'
}

export function isPreviewable(contentType) {
  if (!contentType) return false
  if (contentType.startsWith('image/')) return true
  if (contentType.startsWith('video/')) return true
  if (contentType.startsWith('audio/')) return true
  if (contentType === 'application/pdf') return true
  if (contentType === 'text/plain') return true
  if (contentType === 'text/html') return true
  if (contentType === 'text/css') return true
  if (contentType === 'text/csv') return true
  if (contentType === 'text/markdown') return true
  if (contentType === 'application/json') return true
  if (contentType === 'application/javascript') return true
  return false
}

export function getPreviewType(contentType) {
  if (!contentType) return null
  if (contentType.startsWith('image/')) return 'image'
  if (contentType.startsWith('video/')) return 'video'
  if (contentType.startsWith('audio/')) return 'audio'
  if (contentType === 'application/pdf') return 'pdf'
  if (
    contentType === 'text/plain' ||
    contentType === 'text/html' ||
    contentType === 'text/css' ||
    contentType === 'text/csv' ||
    contentType === 'text/markdown' ||
    contentType === 'application/json' ||
    contentType === 'application/javascript'
  ) return 'code'
  return null
}

/**
 * Map a content type to a lucide icon name (used as `<span class="lucide-...">`).
 * Also used for FeatherIcon component's `name` prop — see individual components.
 */
export function fileIconName(contentType) {
  if (!contentType) return 'file'
  if (contentType.startsWith('image/')) return 'image'
  if (contentType.startsWith('video/')) return 'video'
  if (contentType.startsWith('audio/')) return 'music'
  if (contentType === 'application/pdf') return 'file-text'
  if (contentType === 'application/json') return 'code'
  if (contentType === 'text/html') return 'code'
  if (contentType === 'text/css') return 'code'
  if (contentType === 'application/javascript') return 'code'
  if (contentType === 'text/csv') return 'grid'
  if (contentType === 'text/markdown') return 'file-text'
  if (contentType === 'application/gzip') return 'archive'
  if (contentType === 'font/woff2') return 'type'
  if (contentType.includes('document') || contentType.includes('word')) return 'file-text'
  return 'file'
}
