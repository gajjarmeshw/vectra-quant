import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API base is baked at build time so the installed PWA keeps working
// after the phone leaves the LAN.
export default defineConfig({
  plugins: [react()],
  server: { host: true, port: 5173 },
  build: { outDir: 'dist', sourcemap: false },
})
