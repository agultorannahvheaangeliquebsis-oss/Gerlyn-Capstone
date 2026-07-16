import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Backend (Django) runs on :8000 and already has CORS_ALLOWED_ORIGINS /
// CSRF_TRUSTED_ORIGINS configured for this dev server's origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
});
