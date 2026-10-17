import { readFileSync } from "node:fs";
import { expect, it } from "vitest";
import { END, START, referenceFile, signaturesBlock, writeReference } from "./reference";

it("the skill reference carries the library's signatures verbatim (refresh: OPENUI_REFERENCE_WRITE=1 pnpm -F @lightbulb/shared exec vitest run src/ui/reference.test.ts)", () => {
  if (process.env.OPENUI_REFERENCE_WRITE === "1") writeReference();
  const text = readFileSync(referenceFile(), "utf8");
  const inner = text.slice(text.indexOf(START) + START.length, text.indexOf(END)).trim();
  expect(inner).toBe(signaturesBlock());
});
