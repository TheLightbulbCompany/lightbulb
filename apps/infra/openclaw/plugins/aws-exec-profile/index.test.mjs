// node --test coverage for the aws-exec-profile resolver.
//
// Imports the pure resolver from ./dist/resolve.js (not ./dist/index.js, which
// imports the OpenClaw SDK that only resolves inside the container image).
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { resolve, AWS_READONLY_PROFILE_HEADER } from "./dist/resolve.js";

function withTempDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), "aws-exec-profile-"));
  try {
    return fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("returns AWS_PROFILE when the config file declares the readonly profile", () => {
  withTempDir((dir) => {
    const cfg = join(dir, "config");
    writeFileSync(
      cfg,
      `${AWS_READONLY_PROFILE_HEADER}\nregion = us-east-1\nrole_arn = arn:aws:iam::123:role/x\n`,
    );
    assert.deepEqual(resolve(cfg), { AWS_PROFILE: "isol8-readonly" });
  });
});

test("returns {} when the config path does not exist", () => {
  withTempDir((dir) => {
    const missing = join(dir, "does-not-exist", "config");
    assert.deepEqual(resolve(missing), {});
  });
});

test("returns {} when the file exists but lacks the readonly profile", () => {
  withTempDir((dir) => {
    const cfg = join(dir, "config");
    writeFileSync(cfg, "[profile some-other]\nregion = us-east-1\n");
    assert.deepEqual(resolve(cfg), {});
  });
});
