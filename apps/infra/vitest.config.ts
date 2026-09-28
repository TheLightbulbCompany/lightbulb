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
    // Every test file synths a full stage synchronously, which is CPU-heavy
    // enough that running several files' forks at once starves the main
    // process's event loop — it can miss Vitest's own (unconfigurable,
    // 60s-default) internal RPC heartbeat and throw a false-positive
    // "[vitest-worker]: Timeout calling onTaskUpdate" unhandled error that
    // fails the whole run even though every test passed (seen on the
    // self-hosted CI runner and reproduced locally on an unmodified
    // checkout, always in the default parallel mode and never with file
    // parallelism off). One file at a time removes the contention; ~150-230s
    // serial fits the CI job's 20-minute budget with room to spare.
    fileParallelism: false,
  },
});
