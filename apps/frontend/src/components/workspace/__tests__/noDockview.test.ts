import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

// The dockable shell is gone (one main view per place, one right pane): nothing may bring
// dockview back as an import or a dependency.
function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "node_modules" ? [] : sources(path);
    return /\.(tsx?|css)$/.test(name) ? [path] : [];
  });
}

describe("no dockview", () => {
  it("is imported by no frontend source file", () => {
    const importers = sources(resolve(process.cwd(), "src"))
      .filter((path) => /from\s+["']dockview|import\s+["']dockview|@import\s+["'][^"']*dockview/.test(readFileSync(path, "utf8")));
    expect(importers).toEqual([]);
  });

  it("is not a frontend dependency", () => {
    const pkg = JSON.parse(readFileSync(resolve(process.cwd(), "package.json"), "utf8")) as Record<string, Record<string, string> | undefined>;
    const deps = { ...pkg.dependencies, ...pkg.devDependencies };
    expect(Object.keys(deps).filter((name) => name.includes("dockview"))).toEqual([]);
  });
});
