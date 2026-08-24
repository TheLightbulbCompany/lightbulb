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
    // Relocates os.tmpdir() per test file and deletes it afterwards, so the
    // cloud assembly CDK writes for every `new App()` does not accumulate in
    // /tmp. Read the file — the runner's /tmp is a 16 GB tmpfs and one run of
    // this suite used to leave 1.2 GB of it behind permanently.
    setupFiles: ["./test/scratch-tmpdir.ts"],
    testTimeout: 120_000,
    hookTimeout: 120_000,
  },
});
