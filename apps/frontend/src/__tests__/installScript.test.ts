// @vitest-environment node
import { spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

const SCRIPT = join(__dirname, "../../public/install.sh");

/** Runs install.sh with `uname` stubbed to report `os`. */
function runAs(os: string, args: string[] = []) {
  const bin = mkdtempSync(join(tmpdir(), "uname-"));
  writeFileSync(join(bin, "uname"), `#!/bin/sh\necho ${os}\n`);
  chmodSync(join(bin, "uname"), 0o755);
  return spawnSync("/bin/sh", [SCRIPT, ...args], {
    env: { ...process.env, PATH: `${bin}:/usr/bin:/bin`, HOME: bin },
    encoding: "utf8",
  });
}

describe("curl | sh installer", () => {
  it("is served at /install as plain text", async () => {
    const rewrites = await nextConfig.rewrites!();
    const afterFiles = Array.isArray(rewrites) ? rewrites : rewrites.afterFiles;
    expect(afterFiles).toContainEqual({ source: "/install", destination: "/install.sh" });
    const headers = await nextConfig.headers!();
    for (const source of ["/install", "/install.sh"]) {
      expect(headers).toContainEqual({
        source,
        headers: [{ key: "Content-Type", value: "text/plain; charset=utf-8" }],
      });
    }
  });

  it("parses as POSIX sh", () => {
    expect(spawnSync("/bin/sh", ["-n", SCRIPT]).status).toBe(0);
  });

  it("refuses anything but macOS before touching the network", () => {
    const res = runAs("Linux");
    expect(res.status).toBe(1);
    expect(res.stderr).toContain("macOS only");
  });

  it("refuses unknown options", () => {
    const res = runAs("Darwin", ["--bogus"]);
    expect(res.status).toBe(1);
    expect(res.stderr).toContain("unknown option");
  });
});
