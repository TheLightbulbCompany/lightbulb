import { describe, expect, it } from "vitest";

import { cn, formatBytes, truncate } from "../utils";

describe("truncate", () => {
  it("returns the value untouched when it fits", () => {
    expect(truncate("short", 10)).toBe("short");
    expect(truncate("exactly-10", 10)).toBe("exactly-10");
  });

  it("clips and appends an ellipsis past the limit", () => {
    expect(truncate("abcdefghijk", 5)).toBe("abcde…");
  });

  it("bounds the text before the ellipsis, not the whole string", () => {
    // Callers that need the ellipsis inside a hard budget pass max - 1;
    // callers rely on this (it passes 119 for a 120 budget).
    expect(truncate("abcdefghijk", 5)).toHaveLength(6);
    expect(truncate("abcdefghijk", 4)).toHaveLength(5);
  });

  it("trims whitespace off the clip so the ellipsis does not float", () => {
    expect(truncate("foo      bar", 5)).toBe("foo…");
  });

  it("handles an empty string, which is how nullish callers normalise", () => {
    expect(truncate("", 10)).toBe("");
  });
});

describe("formatBytes", () => {
  it("renders whole bytes without a decimal", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1023)).toBe("1023 B");
  });

  it("scales through KB, MB and GB with one decimal", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(1024 ** 2)).toBe("1.0 MB");
    expect(formatBytes(1024 ** 3)).toBe("1.0 GB");
  });

  it("caps at GB rather than inventing a larger unit", () => {
    expect(formatBytes(5 * 1024 ** 4)).toBe("5120.0 GB");
  });

  it("renders 0 B for missing or nonsensical sizes rather than NaN", () => {
    // Sizes arrive from API payloads where the field is routinely absent.
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(-1)).toBe("0 B");
    expect(formatBytes(Number.NaN)).toBe("0 B");
    expect(formatBytes(Number.POSITIVE_INFINITY)).toBe("0 B");
  });
});

describe("cn", () => {
  it("keeps a theme type step beside a colour class", () => {
    expect(cn("text-ui", "text-[var(--midnight)]")).toBe("text-ui text-[var(--midnight)]");
    expect(cn("text-caption text-muted-foreground")).toBe("text-caption text-muted-foreground");
    expect(cn("text-sm", "text-caption")).toBe("text-caption");
  });
});
