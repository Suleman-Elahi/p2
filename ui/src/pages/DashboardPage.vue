<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Badge, FeatherIcon } from 'frappe-ui'
import { useBucketsSingleton } from '../stores/buckets'
import { useSettingsSingleton } from '../stores/settings'
import { formatBytes } from '../data/files'

const router = useRouter()
const { buckets, bucketStats } = useBucketsSingleton()
const { config } = useSettingsSingleton()

const stats = computed(() => [
  { label: 'Buckets', value: bucketStats.value.total, icon: 'database' },
  { label: 'Total Objects', value: bucketStats.value.totalObjects.toLocaleString(), icon: 'file' },
  { label: 'Total Size', value: formatBytes(bucketStats.value.totalBytes), icon: 'hard-drive' },
])

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
      <h1 class="text-2xl font-semibold text-ink-gray-9">Dashboard</h1>
      <Button variant="solid" theme="gray" icon-left="plus" label="New Bucket" @click="router.push('/buckets')" />
    </header>

    <!-- Content -->
    <div class="mx-auto w-full max-w-[940px] px-3 pt-5 pb-40 sm:px-5">
      <!-- Stats cards -->
      <div class="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <div
          v-for="stat in stats"
          :key="stat.label"
          class="flex items-center gap-3 rounded-md border border-outline-gray-1 bg-surface-white px-4 py-3"
        >
          <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-surface-gray-2">
            <FeatherIcon :name="stat.icon" class="h-4 w-4 text-ink-gray-6" />
          </div>
          <div>
            <p class="text-xs text-ink-gray-5">{{ stat.label }}</p>
            <p class="text-xl font-semibold text-ink-gray-9">{{ stat.value }}</p>
          </div>
        </div>
      </div>

      <!-- All buckets -->
      <section class="mt-8">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-base font-medium text-ink-gray-8">Your Buckets</h2>
          <Button label="View All" variant="ghost" size="sm" @click="router.push('/buckets')" />
        </div>
        <div class="divide-y divide-outline-gray-1 rounded-md border border-outline-gray-1 overflow-hidden">
          <button
            v-for="b in buckets"
            :key="b.name"
            class="flex w-full items-center justify-between px-4 py-3 bg-surface-white text-left hover:bg-surface-gray-1 transition-colors"
            @click="router.push(`/buckets/${b.name}`)"
          >
            <div class="flex items-center gap-3">
              <div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-surface-gray-2">
                <FeatherIcon name="database" class="h-4 w-4 text-ink-gray-6" />
              </div>
              <div>
                <p class="text-sm font-medium text-ink-gray-9">{{ b.name }}</p>
                <p class="text-xs text-ink-gray-5">{{ b.region }}</p>
              </div>
            </div>
            <div class="flex items-center gap-2">
              <Badge :label="b.accessPolicy" :theme="accessTheme(b.accessPolicy)" variant="subtle" size="sm" />
              <FeatherIcon name="chevron-right" class="h-4 w-4 text-ink-gray-4" />
            </div>
          </button>
        </div>
      </section>

      <!-- Connection info -->
      <section class="mt-8">
        <h2 class="mb-3 text-base font-medium text-ink-gray-8">Connection</h2>
        <div class="flex items-center gap-3 rounded-md border border-outline-gray-1 bg-surface-white px-4 py-3">
          <div class="flex h-2 w-2 rounded-full bg-green-500 shrink-0"></div>
          <div>
            <p class="text-sm text-ink-gray-8">Connected to {{ config.s3_endpoint || 'localhost' }}</p>
            <p class="text-xs text-ink-gray-5">Version: {{ config.version }}</p>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>
