import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    // One convention: tests live in a `__tests__/` dir next to the code they
    // cover. The old parallel `tests/unit/**` mirror of `src/` is gone — a
    // grep of the source dir now finds every test that touches it.
    include: ['src/**/__tests__/**/*.test.{ts,tsx}'],
    exclude: ['node_modules/**/*'],
    // Use threads pool with limited workers
    pool: 'threads',
    poolOptions: {
      threads: {
        maxThreads: 2,
        minThreads: 1,
      },
    },
    // Increase test timeout for slow module initialization
    testTimeout: 30000,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      // `server-only` is a Next.js runtime guard that errors on client import.
      // In vitest (node), it's a no-op — alias to an empty stub so server-side
      // modules (e.g. src/app/admin/_lib/url.ts) can be imported for unit tests.
      'server-only': path.resolve(__dirname, './tests/stubs/server-only.ts'),
    },
  },
});
