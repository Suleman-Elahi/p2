import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import frappeui from 'frappe-ui/vite'

export default defineConfig({
  plugins: [
    frappeui({
      frappeProxy: false,
      jinjaBootData: false,
      buildConfig: false,
    }),
    vue(),
  ],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8787',
      '/_/': 'http://localhost:8787',
    },
  },
  optimizeDeps: {
    exclude: ['frappe-ui'],
    include: [
      'feather-icons',
      'tippy.js',
      'showdown',
      'engine.io-client',
      'socket.io-client',
      'debug',
    ],
  },
})
