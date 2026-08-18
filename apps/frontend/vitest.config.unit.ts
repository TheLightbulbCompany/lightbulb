/**
 * Minimal vitest config for pure utility tests that don't need jsdom/MSW/React.
 * Usage: npx vitest run --config vitest.config.unit.ts src/lib/__tests__/tar.test.ts
 *
 * Tests that need DOM rendering can opt-in via `// @vitest-environment jsdom`
 * at the top of the test file. The react plugin is included for JSX transforms.
 *
 * Derived from the main config so the include glob and aliases have exactly one
 * definition — they drifted into byte-identical copies once before.
 */
import { defineConfig, mergeConfig, type UserConfig } from 'vitest/config';
import base from './vitest.config';

const config: UserConfig = mergeConfig(
  base,
  defineConfig({ test: { environment: 'node' } }),
);

// mergeConfig CONCATENATES arrays, so setupFiles has to be replaced outright
// rather than merged: tests/setup.ts is jsdom-only (React Testing Library
// cleanup, jsdom-backed storage) and throws under this config's node
// environment — which is the whole reason this config exists.
config.test!.setupFiles = [];

export default config;
