import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// The annotation panel writes through useTeamsApi (post/patch).
const post = vi.fn();
const patch = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ post, patch }),
}));

import { buildLineDiff } from "../lib/lineDiff";
import { computeAnchor } from "../lib/documentAnnotationSelection";
import { getRequestConfirmationTargetHref } from "../lib/issue-thread-interactions";
import { DocumentAnnotationPanel } from "../DocumentAnnotationPanel";
import type { RequestConfirmationIssueDocumentTarget } from "@/components/teams/shared/types";
import type { DocumentAnnotationThread } from "../lib/documentReviewTypes";

beforeEach(() => {
  post.mockReset();
  patch.mockReset();
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
});

describe("getRequestConfirmationTargetHref", () => {
  const target: RequestConfirmationIssueDocumentTarget = {
    type: "issue_document",
    key: "plan",
    revisionId: "rev1",
    issueId: "iss1",
  };

  it("returns an in-page hash for a same-issue document target", () => {
    expect(getRequestConfirmationTargetHref({ issueId: "iss1", target })).toBe("#document-plan");
  });

  it("falls back to a full path for a cross-issue document target", () => {
    expect(getRequestConfirmationTargetHref({ issueId: "other", target })).toBe(
      "/issues/iss1#document-plan",
    );
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
