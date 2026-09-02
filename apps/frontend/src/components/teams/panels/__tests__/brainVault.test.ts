import { describe, it, expect } from "vitest";
import type { FileEntry } from "@/hooks/useWorkspaceFiles";
import {
  toReadableMarkdown,
  parseFrontmatter,
  pagePills,
  splitRelated,
  parseIndexSections,
  recentlyUpdated,
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
  it("turns obsidian wikilinks into relative markdown links, alias first", () => {
    expect(toReadableMarkdown("see [[entities/ada-lovelace|Ada Lovelace]] and [[ada-lovelace]]"))
      .toBe("see [Ada Lovelace](<entities/ada-lovelace>) and [ada-lovelace](<ada-lovelace>)");
  });
  it("drops a #heading suffix from the target", () => {
    expect(toReadableMarkdown("[[entities/ada#Bio|Ada]]")).toBe("[Ada](<entities/ada>)");
  });
});

describe("parseFrontmatter", () => {
  it("reads the four fields we show and hands back the body (markdown.ts FRONTMATTER_PATTERN)", () => {
    const { meta, body } = parseFrontmatter(
      "---\npageType: entity\nid: e-1\ntitle: Ada\nstatus: draft\nupdatedAt: 2026-08-31T23:30:00Z\n" +
        "sourceIds:\n  - src-1\n  - src-2\nclaims:\n  - text: born 1815\n---\n\n# Ada\n",
    );
    expect(meta).toEqual({
      pageType: "entity",
      status: "draft",
      updatedAt: "2026-08-31T23:30:00Z",
      sourceIds: ["src-1", "src-2"],
    });
    expect(body).toBe("\n# Ada\n");
  });
  it("reads an inline list and ignores keys we don't render", () => {
    expect(parseFrontmatter('---\nsourceIds: ["a", "b"]\nconfidence: 0.4\n---\nx').meta).toEqual({
      sourceIds: ["a", "b"],
    });
  });
  it("no frontmatter is empty meta and an untouched body", () => {
    expect(parseFrontmatter("# Ada\n")).toEqual({ meta: {}, body: "# Ada\n" });
  });
});

describe("pagePills", () => {
  it("orders the four; the date is pinned to en-US/UTC", () => {
    // 23:30Z is Aug 31 in UTC and Sep 1 in Tokyo — this case fails if the pin is dropped.
    expect(
      pagePills({
        pageType: "source",
        status: "verified",
        updatedAt: "2026-08-31T23:30:00Z",
        sourceIds: ["a"],
      }),
    ).toEqual(["source", "verified", "Updated Aug 31, 2026", "1 source"]);
  });
  it("omits what the page doesn't carry; an unparseable date is silent", () => {
    expect(pagePills({})).toEqual([]);
    expect(pagePills({ updatedAt: "soon", sourceIds: [] })).toEqual([]);
    expect(pagePills({ sourceIds: ["a", "b"] })).toEqual(["2 sources"]);
  });
});

describe("splitRelated", () => {
  it("lifts the trailing Related list out of the body", () => {
    const { body, related } = splitRelated(
      "# Ada\n\nText.\n\n## Related\n\n- [[entities/babbage|Charles Babbage]]\n- [[concepts/analytical-engine]]\n",
    );
    expect(body).toBe("# Ada\n\nText.\n\n");
    expect(related).toEqual([
      { target: "entities/babbage", alias: "Charles Babbage" },
      { target: "concepts/analytical-engine", alias: "concepts/analytical-engine" },
    ]);
  });
  it("keeps a section that follows Related in the body", () => {
    const { body, related } = splitRelated("a\n\n## Related\n\n- [[x|X]]\n\n## Sources\n\nb\n");
    expect(body).toBe("a\n\n## Sources\n\nb\n");
    expect(related).toHaveLength(1);
  });
  it("reads the block compile actually writes: nested sub-headings, both markers, every link", () => {
    // memory-wiki's own output — buildRelatedBlockBody's `### Sources` /
    // `### Referenced By` / `### Related Pages` nested INSIDE the managed
    // block, with `## Related` on the line above the start marker
    // (plugin-sdk/memory-host-markdown.ts). Bounding on ANY heading ends the
    // section at the first `###`: no chips, and an orphaned `## Related`.
    const { body, related } = splitRelated(
      [
        "# Ada Lovelace",
        "",
        "She wrote the first algorithm.",
        "",
        "## Related",
        "<!-- openclaw:wiki:related:start -->",
        "### Sources",
        "- [[sources/alpha|Alpha]]",
        "### Referenced By",
        "- [[entities/beta|Beta]]",
        "### Related Pages",
        "- [[concepts/gamma|Gamma]]",
        "<!-- openclaw:wiki:related:end -->",
        "",
      ].join("\n"),
    );
    expect(body).toBe("# Ada Lovelace\n\nShe wrote the first algorithm.\n\n");
    expect(related).toEqual([
      { target: "sources/alpha", alias: "Alpha" },
      { target: "entities/beta", alias: "Beta" },
      { target: "concepts/gamma", alias: "Gamma" },
    ]);
  });
  it("compile's empty body — the sentinel — is no chips and a clean page", () => {
    const { body, related } = splitRelated(
      [
        "# Ada",
        "",
        "## Related",
        "<!-- openclaw:wiki:related:start -->",
        "- No related pages yet.",
        "<!-- openclaw:wiki:related:end -->",
        "",
      ].join("\n"),
    );
    expect(body).toBe("# Ada\n\n");
    expect(related).toEqual([]);
  });
  it("leaves a linkless Related section the OWNER wrote exactly where it is", () => {
    const page = "# Ada\n\n## Related\n\nAsk Charles about the engine notes.\n";
    expect(splitRelated(page)).toEqual({ body: page, related: [] });
  });

  it("lifts the compiler's trailing block, not an owner's earlier Related subsection", () => {
    const page =
      "# Ada\n\n## Notes\n\n### Related\n\n- [[entities/acme|Acme]]\n\n## Related\n" +
      "<!-- openclaw:wiki:related:start -->\n### Sources\n- [[sources/deck|Deck]]\n<!-- openclaw:wiki:related:end -->\n";
    const { body, related } = splitRelated(page);
    expect(related).toEqual([{ target: "sources/deck", alias: "Deck" }]);
    expect(body).toContain("### Related\n\n- [[entities/acme|Acme]]");
    expect(body).not.toContain("openclaw:wiki:related");
  });

  it("keeps a Related section the owner structured with their own sub-headings", () => {
    const page =
      "# Ada\n\n## Related\n\n### Customers\n- [[entities/acme|Acme]]\n### Competitors\n- [[entities/globex|Globex]]\n";
    expect(splitRelated(page)).toEqual({ body: page, related: [] });
  });

  it("keeps a Related section that annotates its links — chips cannot carry the why", () => {
    const page =
      "# Ada\n\n## Related\n\n- [[entities/babbage|Charles Babbage]] — funded the first engine.\n";
    expect(splitRelated(page)).toEqual({ body: page, related: [] });
  });
  it("an empty Related section leaves no links and no heading (the plugin's markers carry none)", () => {
    expect(
      splitRelated(
        "a\n\n## Related\n\n<!-- openclaw:wiki:related:start -->\n<!-- openclaw:wiki:related:end -->\n",
      ),
    ).toEqual({ body: "a\n\n", related: [] });
  });
  it("no Related section is a no-op", () => {
    expect(splitRelated("# Ada\n")).toEqual({ body: "# Ada\n", related: [] });
  });
});

describe("parseIndexSections", () => {
  const COMPILED = [
    "# Wiki Index",
    "",
    "## Generated",
    "<!-- openclaw:wiki:index:start -->",
    "### Sources",
    "- [[sources/deck|Pitch deck]]",
    "- [[sources/notes|Call notes]]",
    "### Entities",
    "- [[entities/ada-lovelace|Ada Lovelace]]",
    "<!-- openclaw:wiki:index:end -->",
  ].join("\n");

  it("reads the managed block into sections whose count is the list beside it", () => {
    expect(parseIndexSections(COMPILED)).toEqual([
      {
        section: "Sources",
        count: 2,
        links: [
          { target: "sources/deck", alias: "Pitch deck" },
          { target: "sources/notes", alias: "Call notes" },
        ],
      },
      {
        section: "Entities",
        count: 1,
        links: [{ target: "entities/ada-lovelace", alias: "Ada Lovelace" }],
      },
    ]);
  });
  it("ignores headings outside the managed block", () => {
    expect(
      parseIndexSections(
        "### Not generated\n- [[x|X]]\n<!-- openclaw:wiki:index:start -->\n<!-- openclaw:wiki:index:end -->",
      ),
    ).toEqual([]);
  });
  it("is empty with no managed block, and on the scaffold block — the caller renders the page", () => {
    expect(parseIndexSections("# Wiki Index\n\n- [[sources/deck|Pitch deck]]\n")).toEqual([]);
    expect(parseIndexSections("")).toEqual([]);
    // vault.ts, the block a brand-new vault ships with.
    expect(
      parseIndexSections(
        "# Wiki Index\n\n## Generated\n<!-- openclaw:wiki:index:start -->\n- No compiled pages yet.\n<!-- openclaw:wiki:index:end -->",
      ),
    ).toEqual([]);
  });
  it("keeps a section compile wrote with nothing under it", () => {
    expect(
      parseIndexSections("<!-- openclaw:wiki:index:start -->\n### Concepts\n<!-- openclaw:wiki:index:end -->"),
    ).toEqual([{ section: "Concepts", count: 0, links: [] }]);
  });
});

describe("recentlyUpdated", () => {
  it("newest first, filed pages only — never inbox/, never an index.md", () => {
    const t = (path: string, modified_at: number): FileEntry => ({ ...f(path), modified_at });
    const files = [
      t("entities/ada-lovelace.md", 300),
      t("index.md", 900),
      t("entities/index.md", 800),
      t("inbox/deck.pdf", 700),
      // compile rewrites its dashboards on essentially every wiki_apply, so a
      // reports/ page is always the newest thing in the vault. It is never
      // what "recently updated" means.
      t("reports/open-questions.md", 950),
      t("sources/deck.md", 500),
      t("concepts/engine.md", 100),
    ];
    expect(recentlyUpdated(files, 2).map((e) => e.path)).toEqual([
      "sources/deck.md",
      "entities/ada-lovelace.md",
    ]);
    expect(recentlyUpdated(files, 5).map((e) => e.path)).toEqual([
      "sources/deck.md",
      "entities/ada-lovelace.md",
      "concepts/engine.md",
    ]);
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
