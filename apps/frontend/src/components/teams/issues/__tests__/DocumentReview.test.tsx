import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// The annotation panel writes through useTeamsApi (post/patch); DocumentRow also
// reads annotations via `read`, so the mock exposes all three.
const post = vi.fn();
const patch = vi.fn();
const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ post, patch, read }),
}));

import {
  buildLineDiff,
  isLineDiffTooLarge,
  LINE_DIFF_MAX_LINES,
  LINE_DIFF_FALLBACK_MAX_ROWS,
} from "../lib/lineDiff";
import { computeAnchor, rangesForText } from "../lib/documentAnnotationSelection";
import { DocumentAnnotationPanel } from "../DocumentAnnotationPanel";
import { DocumentDiffModal } from "../DocumentDiffModal";
import { DocumentRow } from "../DocumentRow";
import type { CompanyAgent, IssueDocument } from "@/components/teams/shared/types";
import type { DocumentAnnotationThread, DocumentRevision } from "../lib/documentReviewTypes";

beforeEach(() => {
  post.mockReset();
  patch.mockReset();
  read.mockReset();
});

describe("buildLineDiff", () => {
  it("marks added, removed, and context lines", () => {
    const rows = buildLineDiff("a\nb\nc", "a\nB\nc");
    const kinds = rows.map((r) => r.kind);
    expect(kinds).toContain("context");
    expect(kinds).toContain("removed");
    expect(kinds).toContain("added");
    // The unchanged first + last lines are context.
    expect(rows[0]).toMatchObject({ kind: "context", text: "a" });
    expect(rows.find((r) => r.kind === "removed")?.text).toBe("b");
    expect(rows.find((r) => r.kind === "added")?.text).toBe("B");
  });

  it("returns all-context for identical text", () => {
    const rows = buildLineDiff("x\ny", "x\ny");
    expect(rows.every((r) => r.kind === "context")).toBe(true);
  });

  it("keeps the fine LCS diff for normal-sized docs", () => {
    expect(isLineDiffTooLarge(3, 3)).toBe(false);
  });

  it("caps the oversized fallback instead of materializing a row per line", () => {
    const lineCount = LINE_DIFF_MAX_LINES + 1;
    expect(isLineDiffTooLarge(lineCount, lineCount)).toBe(true);

    // Feed IDENTICAL over-threshold text. The LCS path would return all-context
    // rows for identical input (see the test above), and building its
    // (lineCount+1)^2 DP matrix is exactly the freeze we're guarding against.
    // Zero context rows prove the guard skipped the matrix; a bounded row count
    // proves the fallback no longer emits one row per line (which for a >1M-line
    // body would freeze the tab just as badly as the matrix).
    const big = Array.from({ length: lineCount }, (_, i) => `line ${i}`).join("\n");
    const rows = buildLineDiff(big, big);
    expect(rows.some((r) => r.kind === "context")).toBe(false);
    // Bounded: at most the cap of content rows + one truncation summary row.
    expect(rows.length).toBeLessThanOrEqual(LINE_DIFF_FALLBACK_MAX_ROWS + 1);
    expect(rows.filter((r) => r.kind !== "truncated")).toHaveLength(LINE_DIFF_FALLBACK_MAX_ROWS);
    // Both sides are represented within the cap.
    expect(rows.some((r) => r.kind === "removed")).toBe(true);
    expect(rows.some((r) => r.kind === "added")).toBe(true);
    // A single, clear truncation indicator replaces the dropped lines.
    const truncated = rows.filter((r) => r.kind === "truncated");
    expect(truncated).toHaveLength(1);
    expect(truncated[0].text).toMatch(/truncated/i);
    expect(truncated[0].text).toMatch(new RegExp(`${lineCount} lines removed`));
  });
});

describe("computeAnchor", () => {
  it("builds a valid selector with end > start on both spans", () => {
    const markdown = "The quick brown fox jumps over the lazy dog.";
    const containerText = markdown;
    const start = markdown.indexOf("brown fox");
    const anchor = computeAnchor({
      markdown,
      containerText,
      startOffset: start,
      endOffset: start + "brown fox".length,
    });
    expect(anchor).not.toBeNull();
    expect(anchor!.selectedText).toBe("brown fox");
    expect(anchor!.selector.quote.exact).toBe("brown fox");
    const pos = anchor!.selector.position;
    expect(pos.normalizedEnd).toBeGreaterThan(pos.normalizedStart);
    expect(pos.markdownEnd).toBeGreaterThan(pos.markdownStart);
    // Exact-match path pins the markdown offsets to the raw body.
    expect(pos.markdownStart).toBe(start);
    // Prefix/suffix context is captured.
    expect(anchor!.selector.quote.prefix.endsWith("quick ")).toBe(true);
  });

  it("returns null for a whitespace-only selection", () => {
    expect(
      computeAnchor({ markdown: "a b c", containerText: "a b c", startOffset: 1, endOffset: 2 }),
    ).toBeNull();
  });

  describe("repeated selected text", () => {
    // "foo bar" appears at raw offsets 0 and 12 in this document.
    const markdown = "foo bar baz foo bar qux";
    const containerText = markdown;

    it("pins the markdown span to the SECOND occurrence when the user selects it", () => {
      const start = 12;
      const anchor = computeAnchor({
        markdown,
        containerText,
        startOffset: start,
        endOffset: start + "foo bar".length,
      });
      expect(anchor).not.toBeNull();
      const pos = anchor!.selector.position;
      // Not the first occurrence (0) — the actual, later selection.
      expect(pos.markdownStart).toBe(12);
      expect(pos.markdownEnd).toBe(19);
      expect(pos.normalizedStart).toBe(12);
      expect(markdown.slice(pos.markdownStart, pos.markdownEnd)).toBe("foo bar");
    });

    it("still pins the FIRST occurrence when the user selects it", () => {
      const start = 0;
      const anchor = computeAnchor({
        markdown,
        containerText,
        startOffset: start,
        endOffset: start + "foo bar".length,
      });
      expect(anchor).not.toBeNull();
      const pos = anchor!.selector.position;
      expect(pos.markdownStart).toBe(0);
      expect(pos.markdownEnd).toBe(7);
      expect(pos.normalizedStart).toBe(0);
    });

    it("leaves a single-occurrence selection at its only match", () => {
      const single = "the quick brown fox";
      const start = single.indexOf("brown");
      const anchor = computeAnchor({
        markdown: single,
        containerText: single,
        startOffset: start,
        endOffset: start + "brown".length,
      });
      expect(anchor).not.toBeNull();
      expect(anchor!.selector.position.markdownStart).toBe(start);
    });
  });

  describe("hidden markdown-syntax collisions", () => {
    // The selected word also appears inside a link's URL, which is NOT rendered.
    // The rendered container only shows the link label + trailing word.
    const markdown = "[click](https://here.example) here";
    const containerText = "click here"; // what the reader actually sees

    it("anchors to the rendered word, not the same word buried in the URL", () => {
      // "here" is at container offset 6 (the visible trailing word). In the raw
      // markdown it also appears at offset 16 (inside the URL). The old raw
      // indexOf scan could pick offset 16; the projection must pick the rendered
      // occurrence at offset 30.
      const start = containerText.indexOf("here");
      const anchor = computeAnchor({
        markdown,
        containerText,
        startOffset: start,
        endOffset: start + "here".length,
      });
      expect(anchor).not.toBeNull();
      const pos = anchor!.selector.position;
      // The URL "here" starts at 16 — the selector must NOT point there.
      expect(pos.markdownStart).not.toBe(markdown.indexOf("here"));
      expect(pos.markdownStart).toBe(markdown.lastIndexOf("here"));
      expect(markdown.slice(pos.markdownStart, pos.markdownEnd)).toBe("here");
      expect(pos.markdownEnd).toBeGreaterThan(pos.markdownStart);
    });

    it("anchors the link LABEL to the rendered label text", () => {
      // Selecting "click" (the rendered label) resolves inside the `[click]`
      // markup, i.e. to the label text that actually renders.
      const start = containerText.indexOf("click");
      const anchor = computeAnchor({
        markdown,
        containerText,
        startOffset: start,
        endOffset: start + "click".length,
      });
      expect(anchor).not.toBeNull();
      const pos = anchor!.selector.position;
      expect(markdown.slice(pos.markdownStart, pos.markdownEnd)).toBe("click");
      expect(pos.markdownStart).toBe(markdown.indexOf("click"));
    });

    it("anchors a trailing word that ABUTS a link (no space) to the rendered occurrence, not the URL", () => {
      // Regression: the rendered `word` immediately follows the link with NO
      // separating whitespace, so the subsequence walk could keep matching
      // THROUGH the hidden `](https://word.example)` destination and land on the
      // `word` buried inside the URL. The projection must skip link destinations
      // so only rendered positions are candidates.
      const adjMarkdown = "[x](https://word.example)word";
      const adjContainer = "xword"; // link label "x" + trailing rendered "word"
      const start = adjContainer.indexOf("word"); // 1
      const anchor = computeAnchor({
        markdown: adjMarkdown,
        containerText: adjContainer,
        startOffset: start,
        endOffset: start + "word".length,
      });
      expect(anchor).not.toBeNull();
      const pos = anchor!.selector.position;
      // The URL "word" starts at 12; the selector must point at the trailing
      // rendered occurrence (25), never inside the destination.
      expect(pos.markdownStart).not.toBe(adjMarkdown.indexOf("word"));
      expect(pos.markdownStart).toBe(adjMarkdown.lastIndexOf("word"));
      expect(adjMarkdown.slice(pos.markdownStart, pos.markdownEnd)).toBe("word");
      expect(pos.markdownEnd).toBeGreaterThan(pos.markdownStart);
    });
  });
});

describe("rangesForText occurrence resolution", () => {
  function makeContainer(text: string): HTMLElement {
    const el = document.createElement("div");
    el.textContent = text;
    // Attach to the document so TreeWalker / Range operate on a live tree.
    document.body.appendChild(el);
    return el;
  }

  it("highlights the first occurrence when no stored offset is supplied", () => {
    // "foo bar" repeats at raw offsets 0 and 12.
    const container = makeContainer("foo bar baz foo bar qux");
    const ranges = rangesForText(container, "foo bar");
    expect(ranges).toHaveLength(1);
    expect(ranges[0].startOffset).toBe(0);
    expect(ranges[0].toString()).toBe("foo bar");
  });

  it("resolves a repeated phrase to the stored offset's occurrence, not the first", () => {
    const container = makeContainer("foo bar baz foo bar qux");
    // Stored normalized offset points at the SECOND occurrence (offset 12).
    const ranges = rangesForText(container, "foo bar", 12);
    expect(ranges).toHaveLength(1);
    expect(ranges[0].startOffset).toBe(12);
    expect(ranges[0].toString()).toBe("foo bar");
  });
});

describe("DocumentAnnotationPanel", () => {
  const baseProps = {
    issueId: "iss1",
    documentKey: "plan",
    baseRevisionId: "rev-uuid",
    baseRevisionNumber: 2,
    agents: [{ id: "ag1", name: "Friday" }] as never,
    onClearPendingAnchor: vi.fn(),
    onClose: vi.fn(),
    focusedThreadId: null,
    onFocusThread: vi.fn(),
    refetch: vi.fn(),
  };

  const thread: DocumentAnnotationThread = {
    id: "t1",
    documentKey: "plan",
    status: "open",
    anchorState: "active",
    selectedText: "some quoted plan text",
    normalizedStart: 3,
    createdAt: "2026-01-01T00:00:00Z",
    comments: [
      {
        id: "c1",
        threadId: "t1",
        body: "please clarify this section",
        authorAgentId: "ag1",
        createdAt: "2026-01-01T00:00:00Z",
      },
    ],
  };

  it("lists the thread quote and reports its comment count when collapsed", () => {
    render(<DocumentAnnotationPanel {...baseProps} threads={[thread]} pendingAnchor={null} />);
    // The quote surfaces, plus a comment-count summary (collapsed).
    expect(screen.getByText("some quoted plan text")).toBeInTheDocument();
    expect(screen.getByText(/1 comment/)).toBeInTheDocument();
    // Clicking the collapsed card asks the (parent-owned) focus to open it.
    fireEvent.click(screen.getByText("some quoted plan text"));
    expect(baseProps.onFocusThread).toHaveBeenCalledWith("t1");
  });

  it("shows comments + resolved author when the thread is focused", () => {
    render(
      <DocumentAnnotationPanel
        {...baseProps}
        threads={[thread]}
        pendingAnchor={null}
        focusedThreadId="t1"
      />,
    );
    expect(screen.getByText("please clarify this section")).toBeInTheDocument();
    // Agent author resolved via the agents map.
    expect(screen.getByText("Friday")).toBeInTheDocument();
  });

  it("resolve toggles status via patch", async () => {
    patch.mockResolvedValue({});
    render(
      <DocumentAnnotationPanel
        {...baseProps}
        threads={[thread]}
        pendingAnchor={null}
        focusedThreadId="t1"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /resolve/i }));
    await waitFor(() => {
      expect(patch).toHaveBeenCalledWith("/issues/iss1/documents/plan/annotations/t1", {
        status: "resolved",
      });
    });
    expect(baseProps.refetch).toHaveBeenCalled();
  });

  it("creating a comment posts the pending anchor's selector", async () => {
    post.mockResolvedValue({ id: "t_new" });
    const pendingAnchor = {
      selectedText: "new selection",
      selector: {
        quote: { exact: "new selection", prefix: "", suffix: "" },
        position: { normalizedStart: 0, normalizedEnd: 13, markdownStart: 0, markdownEnd: 13 },
      },
    };
    render(
      <DocumentAnnotationPanel {...baseProps} threads={[]} pendingAnchor={pendingAnchor} />,
    );
    fireEvent.change(screen.getByTestId("document-annotation-composer"), {
      target: { value: "here is my review comment" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^comment$/i }));
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/issues/iss1/documents/plan/annotations", {
        baseRevisionId: "rev-uuid",
        baseRevisionNumber: 2,
        selector: pendingAnchor.selector,
        body: "here is my review comment",
      });
    });
  });

  it("keeps an orphaned unresolved thread visible and resolvable", async () => {
    patch.mockResolvedValue({});
    const orphanThread: DocumentAnnotationThread = {
      ...thread,
      id: "orphan1",
      anchorState: "orphaned",
      status: "open",
      selectedText: "text that was later deleted",
    };
    render(
      <DocumentAnnotationPanel
        {...baseProps}
        threads={[orphanThread]}
        pendingAnchor={null}
        focusedThreadId="orphan1"
      />,
    );
    // Not filtered out: the quote renders, flagged with an "anchor lost" badge.
    expect(screen.getByText("text that was later deleted")).toBeInTheDocument();
    expect(screen.getByTestId("document-annotation-orphaned-orphan1")).toBeInTheDocument();
    // Resolve still works for an orphaned thread.
    fireEvent.click(screen.getByRole("button", { name: /resolve/i }));
    await waitFor(() => {
      expect(patch).toHaveBeenCalledWith("/issues/iss1/documents/plan/annotations/orphan1", {
        status: "resolved",
      });
    });
  });

  it("still hides an orphaned thread once it is resolved", () => {
    const orphanResolved: DocumentAnnotationThread = {
      ...thread,
      id: "orphan2",
      anchorState: "orphaned",
      status: "resolved",
      selectedText: "resolved and orphaned",
    };
    render(
      <DocumentAnnotationPanel {...baseProps} threads={[orphanResolved]} pendingAnchor={null} />,
    );
    expect(screen.queryByText("resolved and orphaned")).not.toBeInTheDocument();
    expect(screen.getByText(/No comments yet/)).toBeInTheDocument();
  });

  it("disables the composer while previewing a historical revision", () => {
    render(
      <DocumentAnnotationPanel
        {...baseProps}
        threads={[]}
        pendingAnchor={{
          selectedText: "x",
          selector: {
            quote: { exact: "x", prefix: "", suffix: "" },
            position: { normalizedStart: 0, normalizedEnd: 1, markdownStart: 0, markdownEnd: 1 },
          },
        }}
        newCommentDisabled
        newCommentDisabledReason="Comments are disabled while previewing a historical revision."
      />,
    );
    expect(
      screen.getByText("Comments are disabled while previewing a historical revision."),
    ).toBeInTheDocument();
    expect(screen.getByTestId("document-annotation-composer")).toBeDisabled();
  });
});

describe("DocumentRow unresolved badge", () => {
  const agents = [{ id: "ag1", name: "Friday" }] as CompanyAgent[];

  function baseDoc(): IssueDocument {
    // The trimmed local IssueDocument type omits the latest-revision fields the
    // wire payload carries; DocumentRow reads them via a cast, so supply them.
    return {
      key: "plan",
      title: "Plan",
      body: "hello world",
      latestRevisionId: "rev-1",
      latestRevisionNumber: 2,
    } as unknown as IssueDocument;
  }

  function mountWithThreads(threads: DocumentAnnotationThread[]) {
    read.mockImplementation((path: string | null) => {
      if (path && path.includes("/annotations")) {
        return { data: { threads }, mutate: vi.fn() };
      }
      // The revisions read is null (row collapsed) — irrelevant to the badge.
      return { data: undefined, mutate: vi.fn() };
    });
    return render(<DocumentRow doc={baseDoc()} agents={agents} issueId="iss1" />);
  }

  const orphanOpen: DocumentAnnotationThread = {
    id: "orphan-open",
    documentKey: "plan",
    status: "open",
    anchorState: "orphaned",
    selectedText: "text that was later deleted",
    createdAt: "2026-01-01T00:00:00Z",
    comments: [],
  };

  it("counts an orphaned-open thread in the unresolved badge + aria-label", () => {
    // The overlay skips this thread (no anchor to draw), but the header badge
    // must still advertise it so a live review comment isn't missed.
    mountWithThreads([orphanOpen]);
    const toggle = screen.getByTestId("document-comments-toggle-plan");
    expect(toggle).toHaveAttribute("aria-label", "1 unresolved comments on plan");
    expect(toggle).toHaveTextContent("1");
  });

  it("shows 0 when the only orphaned thread is resolved", () => {
    mountWithThreads([{ ...orphanOpen, id: "orphan-resolved", status: "resolved" }]);
    const toggle = screen.getByTestId("document-comments-toggle-plan");
    expect(toggle).toHaveAttribute("aria-label", "Comments on plan");
    expect(toggle).toHaveTextContent("0");
  });
});

describe("DocumentDiffModal oversized diff", () => {
  function revision(id: string, revisionNumber: number, body: string): DocumentRevision {
    return {
      id,
      key: "plan",
      revisionNumber,
      title: null,
      body,
      changeSummary: null,
      createdByAgentId: null,
      createdByUserId: "u1",
      createdAt: `2026-01-0${revisionNumber}T00:00:00Z`,
    };
  }

  it("renders a bounded, truncated diff instead of one row per line", async () => {
    // Two over-threshold bodies (mostly-short-lines) — the exact shape that used
    // to freeze the tab: the presence fallback would otherwise materialize a row
    // per old + per new line, and the modal would render them all.
    const lineCount = LINE_DIFF_MAX_LINES + 1;
    const leftBody = Array.from({ length: lineCount }, (_, i) => `old ${i}`).join("\n");
    const rightBody = Array.from({ length: lineCount }, (_, i) => `new ${i}`).join("\n");

    read.mockImplementation((key: string | null) => {
      if (key && key.includes("/revisions")) {
        return { data: { revisions: [revision("r1", 1, leftBody), revision("r2", 2, rightBody)] } };
      }
      return { data: undefined };
    });

    render(
      <DocumentDiffModal
        issueId="iss1"
        documentKey="plan"
        latestRevisionNumber={2}
        open
        onOpenChange={vi.fn()}
      />,
    );

    // The truncation notice is shown to the reader...
    expect(await screen.findByTestId("document-diff-truncated")).toBeInTheDocument();
    // ...and the modal renders at most the cap of line rows — never one per line.
    const renderedRows = screen.getAllByTestId("document-diff-row");
    expect(renderedRows.length).toBeLessThanOrEqual(LINE_DIFF_FALLBACK_MAX_ROWS);
  });
});
