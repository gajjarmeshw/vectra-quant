import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev proxy targets local FastAPI backend on port 8000
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/state': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
      '/config': 'http://127.0.0.1:8000',
      '/suggestions': 'http://127.0.0.1:8000',
      '/squareoff': 'http://127.0.0.1:8000',
      '/killswitch': 'http://127.0.0.1:8000',
      '/push': 'http://127.0.0.1:8000',
      '/report': 'http://127.0.0.1:8000',
      '/strategies': 'http://127.0.0.1:8000',
      '/broker': 'http://127.0.0.1:8000',
      '/data': 'http://127.0.0.1:8000',
      '/live': {
        target: 'ws://127.0.0.1:8000',
        ws: true,
      },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
})
