import { createRouter, createWebHistory } from 'vue-router'
import { isAuthenticated, useRefresh } from './stores/auth'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: () => import('./pages/LoginPage.vue'),
      meta: { guest: true },
    },
    {
      path: '/',
      component: () => import('./layouts/AppLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          name: 'Dashboard',
          component: () => import('./pages/DashboardPage.vue'),
        },
        {
          path: 'buckets',
          name: 'Buckets',
          component: () => import('./pages/BucketsPage.vue'),
        },
        {
          path: 'buckets/:name',
          name: 'FileManager',
          component: () => import('./pages/FileManager.vue'),
        },
        {
          path: 'settings',
          name: 'Settings',
          component: () => import('./pages/SettingsPage.vue'),
        },
      ],
    },
  ],
})

router.beforeEach(async (to, from, next) => {
  if (to.meta.requiresAuth && !isAuthenticated.value) {
    const { refresh } = useRefresh()
    const success = await refresh()
    if (success) {
      next()
    } else {
      next({ name: 'Login' })
    }
  } else if (to.meta.guest && isAuthenticated.value) {
    next({ name: 'Dashboard' })
  } else {
    next()
  }
})
