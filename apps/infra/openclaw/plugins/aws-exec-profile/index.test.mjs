// node --test coverage for the aws-exec-profile resolver.
//
// Imports the pure resolver from ./dist/resolve.js (not ./dist/index.js, which
// imports the OpenClaw SDK that only resolves inside the container image).
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

import { resolve, AWS_CONNECTOR_PROFILE_HEADER } from "./dist/resolve.js";

async function withPluginEntry(entryPath, fn) {
  const dir = mkdtempSync(join(tmpdir(), "aws-exec-profile-entry-"));
  try {
    const sdk = join(dir, "node_modules", "openclaw");
    mkdirSync(join(dir, "dist"), { recursive: true });
    mkdirSync(sdk, { recursive: true });
    writeFileSync(
      join(sdk, "package.json"),
      JSON.stringify({
        type: "module",
        exports: { "./plugin-sdk/plugin-entry": "./plugin-entry.js" },
      }),
    );
    writeFileSync(join(sdk, "plugin-entry.js"), "export const definePluginEntry = (x) => x;\n");
    writeFileSync(
      join(dir, "dist", "resolve.js"),
      readFileSync(new URL("./dist/resolve.js", import.meta.url)),
    );
    const target = join(dir, entryPath.startsWith("./dist/") ? "dist/index.js" : "index.js");
    writeFileSync(target, readFileSync(new URL(entryPath, import.meta.url)));
    return await fn((await import(pathToFileURL(target))).default);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

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

for (const entryPath of ["./index.ts", "./dist/index.js"]) {
  test(`${entryPath} registers source-equivalent connector behavior`, async () => {
    await withPluginEntry(entryPath, (entry) => withTempDir((dir) => {
      const cfg = join(dir, "config");
      writeFileSync(cfg, `${AWS_CONNECTOR_PROFILE_HEADER}\nregion = us-east-1\n`);
      const previous = process.env.AWS_CONFIG_FILE;
      process.env.AWS_CONFIG_FILE = cfg;
      try {
        let hook;
        entry.register({
          on(name, handler, options) {
            hook = { name, handler, options };
          },
        });
        assert.equal(hook.name, "resolve_exec_env");
        assert.deepEqual(hook.options, { priority: 50 });
        assert.deepEqual(hook.handler({}, { agentId: "agent_a" }), {
          AWS_PROFILE: "lightbulb-connector",
        });

        writeFileSync(cfg, "[profile foreign]\nregion = us-east-1\n");
        assert.deepEqual(hook.handler({}, { agentId: "agent_a" }), {});
      } finally {
        if (previous === undefined) delete process.env.AWS_CONFIG_FILE;
        else process.env.AWS_CONFIG_FILE = previous;
      }
    }));
  });
}
