import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Deletion sweep for the competition-sheet pivot: the pre-drop hold and the
 * v1/v2 waitlist vocabulary must never resurface in the frontend. The
 * tokens are concatenated so this file can't match itself.
 */
const FORBIDDEN = [
  "launch_" + "pending",
  "Launch" + "SpotCard",
  "idea_" + "full",
  "drop" + "_at",
];

const EXTENSIONS = new Set([".ts", ".tsx", ".css"]);

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      yield* walk(p);
    } else if (EXTENSIONS.has(p.slice(p.lastIndexOf(".")))) {
      yield p;
    }
  }
}

describe("competition-sheet deletion sweep", () => {
  it("no dead launch vocabulary anywhere under src/", () => {
    const src = join(__dirname, "..");
    const offenders: string[] = [];
    for (const file of walk(src)) {
      const text = readFileSync(file, "utf8");
      for (const token of FORBIDDEN) {
        if (text.includes(token)) offenders.push(`${file}: ${token}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
