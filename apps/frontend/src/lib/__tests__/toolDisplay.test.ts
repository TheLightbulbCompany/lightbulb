import { describe, expect, it } from "vitest";
import {
  defaultTitle,
  normalizeToolName,
  resolveToolDisplay,
  shortenPath,
  summarizeToolRun,
  toolRowLabel,
} from "@/lib/toolDisplay";

describe("normalizeToolName", () => {
  it("strips the loopback-MCP prefix CLI backends add", () => {
    // The same tool arrives as `browser` on the native runner and
    // `mcp__openclaw__browser` on claude-cli containers. Without the strip,
    // every CLI-backed container falls through to the generic fallback — the
    // exact "raw tool name on screen" bug this module exists to fix.
    expect(normalizeToolName("mcp__openclaw__browser")).toBe("browser");
    expect(normalizeToolName("mcp__some-other-server__web_search")).toBe("web_search");
    expect(normalizeToolName("browser")).toBe("browser");
  });

  it("never returns empty", () => {
    expect(normalizeToolName(undefined)).toBe("tool");
    expect(normalizeToolName("  ")).toBe("");
  });
});

describe("defaultTitle", () => {
  it("title-cases snake_case, leaving already-uppercase short segments alone", () => {
    expect(defaultTitle("some_new_tool")).toBe("Some New Tool");
    expect(defaultTitle("read_pdf")).toBe("Read Pdf");
    // Matches upstream: a segment is preserved only when it ARRIVES uppercase,
    // so `io` becomes "Io" and `IO` stays "IO". We do not guess acronyms.
    expect(defaultTitle("do_IO_thing")).toBe("Do IO Thing");
    expect(defaultTitle("do_io_thing")).toBe("Do Io Thing");
  });
});

describe("shortenPath", () => {
  it("trims container paths to the tail", () => {
    expect(shortenPath("/mnt/efs/users/user_2abc/workspaces/agent_9/src/app.ts")).toBe(
      "…/src/app.ts",
    );
    expect(shortenPath("app.ts")).toBe("app.ts");
    expect(shortenPath("src/app.ts")).toBe("src/app.ts");
  });
});

describe("resolveToolDisplay", () => {
  it("reads titles from the vendored upstream config", () => {
    expect(resolveToolDisplay("read", {}).title).toBe("Read");
    expect(resolveToolDisplay("web_search", {}).title).toBe("Web Search");
  });

  it("renders a read with a line range", () => {
    const d = resolveToolDisplay("read", {
      path: "/mnt/efs/users/u1/workspaces/a1/core/connection_pool.py",
      offset: 40,
      limit: 40,
    });
    expect(d.detail).toBe("lines 40-79 from …/core/connection_pool.py");
    expect(toolRowLabel(d, true)).toBe("Reading lines 40-79 from …/core/connection_pool.py");
    expect(toolRowLabel(d, false)).toBe("Read lines 40-79 from …/core/connection_pool.py");
  });

  it("treats an offset of 0 as a real value, not an absent one", () => {
    // A falsy-zero check here previously dropped offset:0 entirely, so
    // reading from the top of a file rendered as "first 40 lines of a.ts"
    // instead of the correct "lines 0-39 from a.ts".
    expect(resolveToolDisplay("read", { path: "a.ts", offset: 0, limit: 40 }).detail).toBe(
      "lines 0-39 from a.ts",
    );
  });

  it("shows shell commands verbatim and collapses long pipelines", () => {
    expect(resolveToolDisplay("bash", { command: "git status" }).detail).toBe("git status");
    expect(
      resolveToolDisplay("bash", { command: "cat log | grep ERROR | sort | uniq -c" }).detail,
    ).toBe("cat log → uniq -c (+2 more)");
  });

  it("normalizes the many spellings of a search query arg", () => {
    expect(resolveToolDisplay("web_search", { query: "ecs spot", count: 5 }).detail).toBe(
      "for “ecs spot” (top 5)",
    );
    expect(resolveToolDisplay("web_search", { q: "ecs spot" }).detail).toBe("for “ecs spot”");
    expect(resolveToolDisplay("web_search", { search: "ecs spot", max_results: 3 }).detail).toBe(
      "for “ecs spot” (top 3)",
    );
  });

  it("resolves the CLI-prefixed spelling identically to the native one", () => {
    const native = resolveToolDisplay("web_search", { query: "x" });
    const viaMcp = resolveToolDisplay("mcp__openclaw__web_search", { query: "x" });
    expect(viaMcp).toEqual(native);
  });

  it("uses the per-action label for multiplexed tools", () => {
    // `browser` alone would render 20+ different operations identically.
    const open = resolveToolDisplay("browser", { action: "open", url: "https://isol8.co/chat" });
    expect(open.gerund).toContain("open");
  });

  it("falls back to a title-cased name rather than the raw identifier", () => {
    const d = resolveToolDisplay("some_unmapped_tool", {});
    expect(d.title).toBe("Some Unmapped Tool");
    expect(toolRowLabel(d, true)).not.toContain("some_unmapped_tool");
  });

  it("survives junk args without throwing", () => {
    for (const args of [undefined, null, "string", 42, [], { path: null }]) {
      expect(() => resolveToolDisplay("read", args)).not.toThrow();
    }
  });
});

describe("summarizeToolRun", () => {
  it("counts distinct targets, not calls", () => {
    // Nine edits across three files is "edited 3 files" — what a person means
    // when they ask what changed.
    const edit = (p: string) => ({ display: resolveToolDisplay("edit", { path: p }) });
    const summary = summarizeToolRun([
      edit("a.ts"),
      edit("a.ts"),
      edit("a.ts"),
      edit("b.ts"),
      edit("c.ts"),
    ]);
    expect(summary).toBe("Edited 3 files");
  });

  it("joins kinds and appends failures", () => {
    const summary = summarizeToolRun([
      { display: resolveToolDisplay("read", { path: "a.ts" }) },
      { display: resolveToolDisplay("read", { path: "b.ts" }) },
      { display: resolveToolDisplay("bash", { command: "npm test" }) },
      { display: resolveToolDisplay("bash", { command: "npm run build" }), failed: true },
    ]);
    expect(summary).toBe("Read 2 files · ran 2 commands · 1 failed");
  });

  it("is singular for one", () => {
    expect(summarizeToolRun([{ display: resolveToolDisplay("read", { path: "a.ts" }) }])).toBe(
      "Read 1 file",
    );
  });
});

describe("summarizeToolRun — clause capitalization", () => {
  it("capitalizes only the first clause, whichever kind leads", () => {
    // Regression: `read`'s verb was stored capitalized ("Read"), which reads
    // correctly when it leads but produces "Ran 3 commands · Read 2 files"
    // when it does not. Every verb is stored lowercase; the join capitalizes.
    const summary = summarizeToolRun([
      { display: resolveToolDisplay("bash", { command: "a" }) },
      { display: resolveToolDisplay("bash", { command: "b" }) },
      { display: resolveToolDisplay("read", { path: "x.ts" }) },
      { display: resolveToolDisplay("read", { path: "y.ts" }) },
    ]);
    expect(summary).toBe("Ran 2 commands · read 2 files");
  });
});
