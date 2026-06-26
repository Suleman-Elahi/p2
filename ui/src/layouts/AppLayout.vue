<script setup>
import { inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Tooltip, Avatar, FeatherIcon } from 'frappe-ui'
import { user, logout } from '../stores/auth'

const route = useRoute()
const router = useRouter()

// ── Global theme (provided by App.vue) ──────────────────────────────────
const isDark = inject('isDark')
const toggleDark = inject('toggleDark')

const navItems = [
  { name: 'Dashboard', icon: 'grid', route: '/' },
  { name: 'Buckets', icon: 'database', route: '/buckets' },
  { name: 'Settings', icon: 'settings', route: '/settings' },
]

function isActive(path) {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

function handleLogout() {
  logout()
  router.push({ name: 'Login' })
}
</script>

<template>
  <div class="flex h-screen bg-surface-white text-ink-gray-9">
    <!-- Sidebar -->
    <aside class="flex w-14 flex-col items-center border-r border-outline-gray-1 bg-surface-menu-bar py-3">
      <!-- App icon -->
      <div class="mb-6 flex h-9 w-9 items-center justify-center rounded-lg bg-surface-white border border-outline-gray-1 shadow-sm">
        <FeatherIcon name="hard-drive" class="h-5 w-5 text-ink-gray-7" />
      </div>

      <!-- Nav items -->
      <nav class="flex flex-1 flex-col items-center gap-1">
        <Tooltip v-for="item in navItems" :key="item.name" :text="item.name">
          <button
            class="flex h-9 w-9 items-center justify-center rounded-lg transition-colors"
            :class="isActive(item.route)
              ? 'bg-surface-white text-ink-gray-9 border border-outline-gray-1 shadow-sm'
              : 'text-ink-gray-5 hover:text-ink-gray-8 hover:bg-surface-gray-2'"
            :aria-label="item.name"
            @click="router.push(item.route)"
          >
            <FeatherIcon :name="item.icon" class="h-4 w-4" />
          </button>
        </Tooltip>
      </nav>

      <!-- Bottom actions -->
      <div class="flex flex-col items-center gap-2">
        <Tooltip :text="isDark ? 'Light mode' : 'Dark mode'">
          <button
            class="flex h-9 w-9 items-center justify-center rounded-lg text-ink-gray-5 hover:text-ink-gray-8 hover:bg-surface-gray-2 transition-colors"
            :aria-label="isDark ? 'Switch to light mode' : 'Switch to dark mode'"
            @click="toggleDark"
          >
            <FeatherIcon :name="isDark ? 'sun' : 'moon'" class="h-4 w-4" />
          </button>
        </Tooltip>

        <Tooltip text="Sign out">
          <button
            class="flex h-9 w-9 items-center justify-center rounded-lg text-ink-gray-5 hover:text-ink-gray-8 hover:bg-surface-gray-2 transition-colors"
            aria-label="Sign out"
            @click="handleLogout"
          >
            <FeatherIcon name="log-out" class="h-4 w-4" />
          </button>
        </Tooltip>

        <Avatar :label="user?.username || 'U'" size="sm" class="mt-1" />
      </div>
    </aside>

    <!-- Main content -->
    <main class="flex-1 overflow-y-auto">
      <router-view />
    </main>
  </div>
</template>
