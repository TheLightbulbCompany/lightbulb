import { defineConfig, devices } from '@playwright/test';
import dotenv from 'dotenv';
import path from 'path';

dotenv.config({ path: path.resolve(__dirname, '.env.local') });

export default defineConfig({
  testDir: './tests/e2e',
  // `__tests__/` dirs under tests/e2e hold vitest unit tests for the fixtures
  // and helpers. Playwright's default testMatch would otherwise collect them.
  testIgnore: '**/__tests__/**',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0, // journey tests have destructive side effects — no retries
  workers: 2,
  globalTimeout: 45 * 60 * 1000, // 45 min — Step 3 alone can hit 20 min when scale-to-zero races the gateway WS handshake on a cold start; Stripe Checkout + starter chat add another 5-7 min per flow. Both flows run in parallel (workers=2) so the gate clock is dominated by the slower flow.
  reporter: [
    ['html', { open: 'never' }],
    ['list'],
  ],
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    extraHTTPHeaders: process.env.VERCEL_AUTOMATION_BYPASS_SECRET
      ? { 'x-vercel-protection-bypass': process.env.VERCEL_AUTOMATION_BYPASS_SECRET }
      : {},
  },
  projects: [
    {
      name: 'setup',
      testMatch: /global\.setup\.ts/,
    },
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
      dependencies: ['setup'],
    },
  ],
  // Start local dev server only when no BASE_URL is set (i.e., not running against live dev)
  // For local e2e against localhost: start backend manually with:
  //   cd apps/backend && uv run uvicorn main:app --port 8000
  webServer: !process.env.BASE_URL
    ? [{
        command: 'pnpm run dev',
        // cwd defaults to apps/frontend/ (directory of this config file)
        url: 'http://localhost:3000',
        reuseExistingServer: !process.env.CI,
        timeout: 120000,
      }]
    : undefined,
});
