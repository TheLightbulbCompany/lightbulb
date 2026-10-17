import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { cardLibrary } from "./library";

export const REFERENCE_PATH = "apps/infra/hermes/skills-src/lightbulb/references/ui.md";
export const START = "<!-- openui:signatures:start -->";
export const END = "<!-- openui:signatures:end -->";

/** The "Component Signatures" section of the generated prompt, verbatim: one line per component. */
export function signaturesBlock(): string {
  const prompt = cardLibrary.prompt({});
  const lines = prompt.split("\n");
  const from = lines.findIndex((l) => l.startsWith("## Component Signatures"));
  const to = lines.findIndex((l, i) => i > from && l.startsWith("## "));
  if (from < 0) throw new Error("prompt has no Component Signatures section");
  return lines.slice(from + 1, to < 0 ? undefined : to).join("\n").trim();
}

/** Repo root: this file is packages/shared/src/ui/, four levels down. */
export const referenceFile = (): string => resolve(__dirname, "../../../..", REFERENCE_PATH);

/** Rewrites the text between the markers with the library's signatures; returns the path written. */
export function writeReference(): string {
  const path = referenceFile();
  const current = readFileSync(path, "utf8");
  const start = current.indexOf(START);
  const end = current.indexOf(END);
  if (start < 0 || end < 0) throw new Error(`markers missing in ${path}`);
  writeFileSync(path, current.slice(0, start + START.length) + "\n" + signaturesBlock() + "\n" + current.slice(end));
  return path;
}
