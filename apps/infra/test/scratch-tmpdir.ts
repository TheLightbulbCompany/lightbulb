/**
 * Keeps the CDK assertion suite from stranding its cloud assemblies in /tmp.
 *
 * `new App()` with no `outdir` gets one from cx-api's
 * `determineOutputDirectory`, which is `mkdtempSync(os.tmpdir() + "/cdk.out")`
 * — created eagerly in the App constructor and never removed. This suite
 * builds 54 Apps, so ONE run strands ~1.2 GB of `/tmp/cdk.out*`. On the
 * self-hosted runner /tmp is a 16 GB tmpfs, so ~13 PR runs fill it and the
 * next job on the box — any job — dies with ENOSPC. That is what happened on
 * 2026-08-24; see `.github/workflows/runner-maintenance.yml` for the manual
 * remedy.
 *
 * The fix is to relocate `os.tmpdir()` for the duration of each test FILE and
 * delete that directory when the file finishes. Two reasons for this shape
 * over an explicit `outdir` on each App:
 *
 *  - It leaves CDK's own per-App `mkdtemp` in place, so parallel vitest
 *    workers (default pool `forks`, one file per worker) get disjoint
 *    directories by construction. A single shared `outdir` would have two
 *    workers writing the same `manifest.json` and `<Stack>.template.json`.
 *  - Setting `CDK_OUTDIR` instead would also give every App an outdir, but its
 *    mere presence in the environment flips `App`'s `autoSynth` default to
 *    true, adding a `beforeExit` synth that can recreate the directory after
 *    cleanup has run.
 *
 * Registered as a `setupFiles` entry so it runs before the test file is
 * imported — the assembly directory is created when the App is constructed,
 * not when it is synthesized, and the Apps here are constructed at import
 * time or inside helpers called from the first test.
 */
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll } from "vitest";

const originalTmpdir = process.env.TMPDIR;
const scratch = mkdtempSync(join(tmpdir(), "isol8-infra-test-"));
process.env.TMPDIR = scratch;

afterAll(() => {
  // Restore first: with `isolate: true` a worker process is reused across
  // files, and the next file's copy of this module calls `tmpdir()` again.
  // Leaving it pointed at a directory we are about to delete would ENOENT.
  if (originalTmpdir === undefined) delete process.env.TMPDIR;
  else process.env.TMPDIR = originalTmpdir;
  rmSync(scratch, { recursive: true, force: true });
});
