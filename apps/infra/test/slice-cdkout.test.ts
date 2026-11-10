import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// scripts/slice-cdkout.mjs: each deploy.yml asset job downloads base.tgz plus
// <assetId>.tgz, so the base must hold every manifest and template, and each
// slice exactly its own asset.
const SCRIPT = join(__dirname, "../scripts/slice-cdkout.mjs");
const list = (tgz: string) => execFileSync("tar", ["-tzf", tgz], { encoding: "utf8" }).split("\n").filter(Boolean);

describe("slice-cdkout", () => {
  it("splits cdk.out into a base and one slice per asset id", () => {
    const root = mkdtempSync(join(tmpdir(), "slice-"));
    const cdkout = join(root, "cdk.out");
    mkdirSync(join(cdkout, "assembly-dev"), { recursive: true });
    mkdirSync(join(cdkout, "asset.aaa"));
    writeFileSync(join(cdkout, "asset.aaa", "index.js"), "x");
    writeFileSync(join(cdkout, "asset.bbb.zip"), "z");
    writeFileSync(join(cdkout, "manifest.json"), "{}");
    writeFileSync(join(cdkout, "assembly-dev", "Stack.template.json"), "{}");
    writeFileSync(
      join(cdkout, "assembly-dev", "Stack.assets.json"),
      JSON.stringify({
        files: {
          aaa: { source: { path: "../asset.aaa", packaging: "zip" } },
          bbb: { source: { path: "../asset.bbb.zip", packaging: "file" } },
          tpl: { source: { path: "Stack.template.json", packaging: "file" } },
        },
        dockerImages: { ddd: { source: { directory: "../asset.aaa" } } },
      }),
    );
    const out = join(root, "slices");
    execFileSync("node", [SCRIPT, cdkout, out]);

    const base = list(join(out, "base.tgz"));
    expect(base).toContain("cdk.out/assembly-dev/Stack.assets.json");
    expect(base).toContain("cdk.out/assembly-dev/Stack.template.json");
    expect(base.some((e) => e.includes("asset.aaa") || e.includes("asset.bbb"))).toBe(false);
    expect(list(join(out, "aaa.tgz"))).toContain("cdk.out/asset.aaa/index.js");
    expect(list(join(out, "ddd.tgz"))).toContain("cdk.out/asset.aaa/index.js");
    expect(list(join(out, "bbb.tgz"))).toEqual(["cdk.out/asset.bbb.zip"]);
    expect(list(join(out, "tpl.tgz"))).toEqual([]); // the template rides in the base
  });
});
