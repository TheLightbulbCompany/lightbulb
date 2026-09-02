import { describe, it, expect } from "vitest";
import type { FileEntry } from "@/hooks/useWorkspaceFiles";
import {
  toReadableMarkdown,
  resolveVaultLink,
  vaultCounts,
  searchVault,
  askBrainDraft,
  fileInboxDraft,
  formatVaultDate,
} from "../brainVault";

const f = (path: string, type: "file" | "dir" = "file"): FileEntry => ({
  name: path.slice(path.lastIndexOf("/") + 1),
  path,
  type,
  size: type === "file" ? 10 : null,
  modified_at: 1,
});
const TREE = [
  f("index.md"), f("AGENTS.md"), f(".openclaw-wiki", "dir"), f(".openclaw-wiki/log.jsonl"),
  f("entities", "dir"), f("entities/index.md"), f("entities/ada-lovelace.md"),
  f("reports", "dir"), f("reports/open-questions.md"),
  f("inbox", "dir"), f("inbox/deck.pdf"),
];

describe("toReadableMarkdown", () => {
  it("strips YAML frontmatter (memory-wiki FRONTMATTER_PATTERN)", () => {
    expect(toReadableMarkdown("---\npageType: entity\ntitle: Ada\n---\n\n# Ada\n")).toBe("\n# Ada\n");
  });
  it("turns obsidian wikilinks into relative markdown links, alias first", () => {
    expect(toReadableMarkdown("see [[entities/ada-lovelace|Ada Lovelace]] and [[ada-lovelace]]"))
      .toBe("see [Ada Lovelace](<entities/ada-lovelace>) and [ada-lovelace](<ada-lovelace>)");
  });
  it("drops a #heading suffix from the target", () => {
    expect(toReadableMarkdown("[[entities/ada#Bio|Ada]]")).toBe("[Ada](<entities/ada>)");
  });
});

describe("resolveVaultLink", () => {
  it("leaves external, shell and anchor hrefs alone", () => {
    expect(resolveVaultLink("https://x.y", TREE)).toBeNull();
    expect(resolveVaultLink("/chat?panel=teams-issues", TREE)).toBeNull();
    expect(resolveVaultLink("#top", TREE)).toBeNull();
    expect(resolveVaultLink("mailto:a@b", TREE)).toBeNull();
  });
  it("resolves a vault-rooted target with or without .md", () => {
    expect(resolveVaultLink("entities/ada-lovelace", TREE)).toEqual({ path: "entities/ada-lovelace.md", exists: true });
    expect(resolveVaultLink("entities/ada-lovelace.md", TREE)).toEqual({ path: "entities/ada-lovelace.md", exists: true });
  });
  it("resolves a bare stem the Obsidian way (first file with that name)", () => {
    expect(resolveVaultLink("ada-lovelace", TREE)).toEqual({ path: "entities/ada-lovelace.md", exists: true });
  });
  it("returns a best-guess path flagged exists:false for an unwritten page", () => {
    expect(resolveVaultLink("entities/grace-hopper", TREE)).toEqual({ path: "entities/grace-hopper.md", exists: false });
  });
  it("resolves a non-markdown vault file exactly", () => {
    expect(resolveVaultLink("inbox/deck.pdf", TREE)).toEqual({ path: "inbox/deck.pdf", exists: true });
  });
});

describe("vaultCounts", () => {
  it("filed = .md under QUERY_DIRS minus index.md (what wiki_search sees); inbox separate", () => {
    expect(vaultCounts(TREE)).toEqual({ filed: 2, knowledge: 1, inbox: 1 });
  });
  it("is all zeros on a scaffold-only vault", () => {
    expect(vaultCounts([f("index.md"), f("entities", "dir"), f("entities/index.md")])).toEqual({ filed: 0, knowledge: 0, inbox: 0 });
  });
});

describe("searchVault", () => {
  it("matches on path, case-insensitively, files only, hiding dot-dir plumbing", () => {
    expect(searchVault(TREE, "ADA").map((e) => e.path)).toEqual(["entities/ada-lovelace.md"]);
    expect(searchVault(TREE, "log")).toEqual([]);
    expect(searchVault(TREE, "  ")).toEqual([]);
  });
});

describe("drafts", () => {
  it("quote the query; plural-safe inbox sentence", () => {
    expect(askBrainDraft("pricing")).toContain('"pricing"');
    expect(fileInboxDraft(1)).toMatch(/^There is 1 file/);
    expect(fileInboxDraft(2)).toMatch(/^There are 2 files/);
  });
});

describe("formatVaultDate", () => {
  // POSIX seconds, pinned to en-US/UTC so a runner's locale or timezone can't
  // move the rendered date.
  it("reads modified_at as seconds, in UTC", () => {
    expect(formatVaultDate(1756771200)).toBe("Sep 2, 2025, 12:00 AM");
  });
});
