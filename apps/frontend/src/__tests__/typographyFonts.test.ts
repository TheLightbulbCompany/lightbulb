import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..");
const read = (path: string) => readFileSync(join(SRC, path), "utf8");

describe("shared typography fonts", () => {
  it("self-hosts Satoshi as the one face in the root layout", () => {
    const layout = read("app/layout.tsx");

    expect(layout).toMatch(/import localFont from "next\/font\/local";/);
    expect(layout).not.toMatch(/\bInter\b/);
    expect(layout).toMatch(/src:\s*"\.\.\/fonts\/satoshi-variable\.woff2"/);
    expect(layout).toMatch(/variable:\s*"--font-satoshi"/);
    expect(layout).toMatch(/className=\{`\$\{satoshi\.variable\} \$\{geistMono\.variable\}/);
  });

  it("uses shared UI and body tokens with resilient fallbacks", () => {
    const css = read("app/globals.css");

    expect(css).toMatch(/--font-sans:\s*var\(--font-satoshi\),\s*"Avenir Next",\s*"Segoe UI",\s*sans-serif;/);
    expect(css).toMatch(/--font-body:\s*var\(--font-sans\);/);
    expect(css).not.toMatch(/--font-inter/);
    expect(css).toMatch(/--font-source-sans:\s*var\(--font-sans\);/);
    expect(css).toMatch(/--font-lora-serif:\s*var\(--font-sans\);/);
    expect(css).toMatch(/body\s*\{[^}]*font-family:\s*var\(--font-sans\);/s);
    expect(css).toMatch(/:where\(\.prose,\s*\.long-form,\s*\.blog-prose\)\s*\{[^}]*font-family:\s*var\(--font-body\);/s);
  });
});
