import { describe, expect, it } from "vitest";
import {
  defaultTitle,
  normalizeToolName,
  resolveToolDisplay,
  shortenPath,
  summarizeToolRun,
  toolRowLabel,
  collapseRepeats,
  redactToolArgs,
} from "./index";

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
    const open = resolveToolDisplay("browser", { action: "open", url: "https://flickmeon.ai/chat" });
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

describe("searchDetail — inner quotes", () => {
  it("does not double up when the query itself quotes an exact phrase", () => {
    // Observed live on the landing demo: for “"northwind ceramics" exist…
    const d = resolveToolDisplay("web_search", {
      query: '"northwind ceramics" existing brands',
    });
    expect(d.detail).toBe("for “'northwind ceramics' existing brands”");
    expect(d.detail).not.toContain('“"');
  });

  it("normalizes curly inner quotes too, so they cannot collide with the wrapper", () => {
    const d = resolveToolDisplay("web_search", { query: "“exact” phrase" });
    expect(d.detail).toBe("for “'exact' phrase”");
  });

  it("leaves an unquoted query untouched", () => {
    expect(resolveToolDisplay("web_search", { query: "ecs spot capacity" }).detail).toBe(
      "for “ecs spot capacity”",
    );
  });
});

describe("Hermes and paired-Mac labels", () => {
  const label = (name: string, args: unknown) => toolRowLabel(resolveToolDisplay(name, args), false);
  const mac = (args: Record<string, unknown>) => label("lightbulb_desktop", { deviceId: "d", generation: "g", ...args });

  it("reads lightbulb_desktop calls as the action they took", () => {
    expect(mac({ action: "browser", browserAction: "open", args: { url: "https://www.supademo.com/demo" } }))
      .toBe("Mac browser · open supademo.com/demo");
    expect(mac({ action: "browser", browserAction: "click", args: { ref: "e12" } })).toBe("Mac browser · click");
    expect(mac({ action: "browser", browserAction: "screenshot", args: {} })).toBe("Mac browser · screenshot");
    expect(mac({ action: "invoke", command: "system.run", args: { argv: ["ls", "~/Projects"], cwd: "/" } }))
      .toBe("Mac shell · ls ~/Projects");
    expect(mac({ action: "status", receiptId: "r1" })).toBe("Mac · check progress");
    expect(mac({ action: "file", fileAction: "dir.list", args: { path: "/Users/p/Projects/app" } }))
      .toBe("Mac files · list …/Projects/app");
    expect(mac({ action: "mcp_call", server: "linear", tool: "list_issues", catalogVersion: "1", arguments: {} }))
      .toBe("Mac MCP · linear list_issues");
  });

  it("never puts typed text in a label", () => {
    expect(mac({ action: "browser", browserAction: "type", args: { ref: "e3", text: "hunter2" } }))
      .toBe("Mac browser · type into a field");
    expect(label("browser_type", { ref: "e3", text: "hunter2" })).toBe("Typed into a field");
  });

  it("names the generic Hermes tools as actions", () => {
    expect(label("skill_view", { name: "lightbulb" })).toBe("Read skill: lightbulb");
    expect(label("tool_describe", { name: "lightbulb_desktop" })).toBe("Looked up a tool");
    expect(label("execute_code", { code: "print(1)" })).toBe("Ran code");
    expect(label("lightbulb_local_sessions", { action: "read" })).toBe("Shared terminal · read output");
  });

  it("masks typed secrets in args before any surface keeps them", () => {
    const typed = redactToolArgs("lightbulb_desktop", {
      action: "browser", browserAction: "type", args: { ref: "e3", text: "hunter2" },
    });
    expect(JSON.stringify(typed)).not.toContain("hunter2");
    const filled = redactToolArgs("lightbulb_desktop", {
      action: "browser", browserAction: "fill", args: { fields: [{ ref: "e1", type: "text", value: "pw" }] },
    });
    expect(JSON.stringify(filled)).not.toContain('"pw"');
    const run = redactToolArgs("lightbulb_desktop", {
      action: "invoke", command: "system.run", args: { argv: ["x"], cwd: "/", env: { TOKEN: "sk-1" } },
    });
    expect(JSON.stringify(run)).not.toContain("sk-1");
    expect(redactToolArgs("browser_type", { ref: "e3", text: "hunter2" }).text).toBe("•••");
  });
});

describe("collapseRepeats", () => {
  it("folds consecutive equal keys, keeping the last item and a count", () => {
    const out = collapseRepeats(["a1", "a2", "b1", "a3"], (s) => s[0]!);
    expect(out).toEqual([{ item: "a2", count: 2 }, { item: "b1", count: 1 }, { item: "a3", count: 1 }]);
  });
});
