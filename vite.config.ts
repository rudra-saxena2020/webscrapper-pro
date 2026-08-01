import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import {defineConfig, loadEnv} from 'vite';

export default defineConfig(({mode}) => {
  const env = loadEnv(mode, '.', '');
  return {
    plugins: [react(), tailwindcss()],
    define: {
      'process.env.GEMINI_API_KEY': JSON.stringify(env.GEMINI_API_KEY),
    },
    resolve: {
      alias: {
        '@': path.resolve(__dirname, '.'),
      },
    },
    optimizeDeps: {
      entries: ['index.html'],
    },
    server: {
      host: '0.0.0.0',
      port: 5000,
      allowedHosts: true,
      watch: {
        ignored: [
          '**/scraped_files/**',
          '**/.local/**',
          '**/.pythonlibs/**',
          '**/.cache/**',
          '**/node_modules/**',
          '**/venv/**',
          '**/artifacts/**',
          '**/__pycache__/**',
          '**/*.py',
          '**/*.csv',
          '**/*.db',
          '**/.env',
          '**/.env.*',
        ]
      }
    },
    build: {
      chunkSizeWarningLimit: 600,
      rollupOptions: {
        output: {
          manualChunks: {
            'react-vendor': ['react', 'react-dom'],
            'query':        ['@tanstack/react-query'],
            'motion':       ['motion'],
            'utils':        ['axios', 'papaparse', 'clsx', 'tailwind-merge'],
            'icons':        ['lucide-react'],
          },
        },
      },
    },
  };
});
