<script setup>
import { ref, watch } from 'vue'
import { FrappeUIProvider, Dialogs } from 'frappe-ui'

// ── Global theme — persisted in localStorage ──────────────────────────
const isDark = ref(localStorage.getItem('p2_dark_mode') === 'true')

function applyTheme() {
  document.documentElement.setAttribute('data-theme', isDark.value ? 'dark' : 'light')
  localStorage.setItem('p2_dark_mode', String(isDark.value))
}
applyTheme()

// Expose toggle for any component to use (e.g. AppLayout sidebar button)
function toggleDark() {
  isDark.value = !isDark.value
  applyTheme()
}

// Provide to descendants
import { provide } from 'vue'
provide('toggleDark', toggleDark)
provide('isDark', isDark)
</script>

<template>
  <FrappeUIProvider>
    <router-view />
    <Dialogs />
  </FrappeUIProvider>
</template>
