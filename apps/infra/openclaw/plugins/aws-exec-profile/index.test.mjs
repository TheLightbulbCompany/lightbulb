// node --test coverage for the aws-exec-profile resolver.
//
// Imports the pure resolver from ./dist/resolve.js (not ./dist/index.js, which
// imports the OpenClaw SDK that only resolves inside the container image).
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { resolve, AWS_CONNECTOR_PROFILE_HEADER } from "./dist/resolve.js";

function withTempDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), "aws-exec-profile-"));
  try {
    return fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("returns AWS_PROFILE when the config file declares the connector profile", () => {
  withTempDir((dir) => {
    const cfg = join(dir, "config");
    writeFileSync(
      cfg,
      `${AWS_CONNECTOR_PROFILE_HEADER}\nregion = us-east-1\nrole_arn = arn:aws:iam::123:role/x\n`,
    );
    assert.deepEqual(resolve(cfg), { AWS_PROFILE: "lightbulb-connector" });
  });
});

test("returns {} when the config path does not exist", () => {
  withTempDir((dir) => {
    const missing = join(dir, "does-not-exist", "config");
    assert.deepEqual(resolve(missing), {});
  });
});

test("returns {} when the file exists but lacks the connector profile", () => {
  withTempDir((dir) => {
    const cfg = join(dir, "config");
    writeFileSync(cfg, "[profile some-other]\nregion = us-east-1\n");
    assert.deepEqual(resolve(cfg), {});
  });
});

test("returns {} when the header appears only as a comment", () => {
  withTempDir((dir) => {
    const cfg = join(dir, "config");
    writeFileSync(cfg, `# ${AWS_CONNECTOR_PROFILE_HEADER}\nregion = us-east-1\n`);
    assert.deepEqual(resolve(cfg), {});
  });
});

test("returns {} for undefined/empty path (falls back to a nonexistent default)", () => {
  // No AWS_CONFIG_FILE -> DEFAULT_AWS_CONFIG_PATH, which does not exist in the
  // test environment, so the read throws and degrades to {}.
  assert.deepEqual(resolve(undefined), {});
  assert.deepEqual(resolve(""), {});
});
