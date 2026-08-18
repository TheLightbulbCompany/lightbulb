/**
 * Minimal vitest config for pure utility tests that don't need jsdom/MSW/React.
 * Usage: npx vitest run --config vitest.config.unit.ts src/lib/__tests__/tar.test.ts
 *
 * Tests that need DOM rendering can opt-in via `// @vitest-environment jsdom`
 * at the top of the test file. The react plugin is included for JSX transforms.
 */
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'node',
    include: ['{src,tests/e2e}/**/__tests__/**/*.test.{ts,tsx}'],
    exclude: ['tests/e2e/**/*.spec.ts', 'tests/e2e/global.setup.ts', 'node_modules/**/*'],
    testTimeout: 30000,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
});
