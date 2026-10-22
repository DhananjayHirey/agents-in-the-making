import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The browser only ever talks to books-backend: /auth for login, /api/agent for the agent relay.
// In dev, Vite proxies both so the page and the API share an origin.
const backend = 'http://localhost:4000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/auth': backend,
      '/api': backend,
    },
  },
});
