import { defineConfig } from "vitest/config";

// CDK assertion tests are plain synchronous synth-and-assert — no DOM, no
// mocking — so `globals: true` keeps describe/it/expect ambient and the suite
// moved off jest without edits.
//
// The timeouts are not padding: synthesising a full Isol8Stage is genuinely
// slow (the ses-inbound cases measure 6-18s each, and several suites synth in
// a beforeAll). The 5s/10s defaults fail those on duration alone.
export default defineConfig({
  test: {
    globals: true,
    environment: "node",
    include: ["test/**/*.test.ts"],
    testTimeout: 120_000,
    hookTimeout: 120_000,
  },
});
