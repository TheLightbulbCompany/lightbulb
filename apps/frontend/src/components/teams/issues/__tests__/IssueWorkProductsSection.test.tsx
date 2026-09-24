import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { SWRResponse } from "swr";

// Per-path read stub + write spies: the section reads work products and
// documents, PATCHes review state and POSTs review-document through useTeamsApi.
let reads: Record<string, Partial<SWRResponse<unknown, Error>>> = {};
const patch = vi.fn();
const post = vi.fn();
const download = vi.fn();
const swrMutate = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) =>
      reads[path] ?? { data: undefined, isLoading: false, error: undefined },
    patch,
    post,
    download,
    // Revalidation goes through useTeamsApi's scoped mutate.
    mutate: swrMutate,
  }),
}));

import { IssueWorkProductsSection } from "../IssueWorkProductsSection";

const ATT = "11111111-1111-4111-8111-111111111111";
const WP_LIST = "/issues/iss1/work-products";

function listOf(...workProducts: Record<string, unknown>[]) {
  reads[WP_LIST] = { data: { workProducts }, isLoading: false, error: undefined };
}

function attachmentMeta(contentType: string, filename: string, byteSize = 2048) {
  const p = `/api/attachments/${ATT}/content`;
  return {
    attachmentId: ATT,
    contentType,
    byteSize,
    contentPath: p,
    openPath: p,
    downloadPath: `${p}?download=1`,
    originalFilename: filename,
  };
}

const base = {
  issueId: "iss1",
  provider: "paperclip",
  url: null,
  status: "active",
  reviewState: "none",
  createdAt: "2026-01-01T00:00:00Z",
};

beforeEach(() => {
  reads = {};
  patch.mockReset();
  post.mockReset();
  download.mockReset();
  download.mockResolvedValue(new Blob(["x"]));
  swrMutate.mockReset();
  URL.createObjectURL = vi.fn(() => "blob:mock");
  URL.revokeObjectURL = vi.fn();
});

describe("IssueWorkProductsSection", () => {
  it("renders nothing when there are no work products (no error, not loading)", () => {
    listOf();
    const { container } = render(<IssueWorkProductsSection issueId="iss1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows attachment outputs as the Output card and other deliverables as work products", () => {
    listOf(
      { ...base, id: "wp-pdf", type: "artifact", title: "Report", isPrimary: true, metadata: attachmentMeta("application/pdf", "report.pdf") },
      { ...base, id: "wp-prev", type: "preview_url", title: "Deploy preview", url: "https://preview.example.com" },
    );
    render(<IssueWorkProductsSection issueId="iss1" />);

    expect(screen.getByText("Output")).toBeInTheDocument();
    expect(screen.getByText("report.pdf")).toBeInTheDocument();
    expect(screen.getByText("Work products")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open preview: deploy preview/i })).toHaveAttribute(
      "href",
      "https://preview.example.com",
    );
    // The output is not repeated as a work-product card.
    expect(screen.queryByTestId("task-chat-rich-work-product-artifact")).not.toBeInTheDocument();
  });

  it.each([
    ["data:", "data:text/html,<script>alert(1)</script>"],
    ["javascript:", "javascript:alert(1)"],
  ])("does not render a live link for an unsafe %s url", (_label, url) => {
    listOf({ ...base, id: "wp-unsafe", type: "preview_url", title: "Sketchy preview", url });
    render(<IssueWorkProductsSection issueId="iss1" />);
    expect(screen.getByText("Sketchy preview")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(document.querySelector(`a[href="${url}"]`)).toBeNull();
  });

  it("Approve PATCHes reviewState=approved and revalidates the list", async () => {
    patch.mockResolvedValue({});
    listOf({ ...base, id: "wp2", type: "pull_request", title: "Fix login", url: "https://github.com/x/y/pull/1", status: "ready_for_review", reviewState: "needs_board_review" });
    render(<IssueWorkProductsSection issueId="iss1" />);

    expect(screen.getByText("Review")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => {
      expect(patch).toHaveBeenCalledWith("/issues/iss1/work-products/wp2", { reviewState: "approved" });
    });
    expect(swrMutate).toHaveBeenCalledWith("/teams/issues/iss1/work-products");
  });

  it("Request changes PATCHes reviewState=changes_requested", async () => {
    patch.mockResolvedValue({});
    listOf({ ...base, id: "wp3", type: "pull_request", title: "Fix login", url: "https://github.com/x/y/pull/1", reviewState: "needs_board_review" });
    render(<IssueWorkProductsSection issueId="iss1" />);
    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
    await waitFor(() => {
      expect(patch).toHaveBeenCalledWith("/issues/iss1/work-products/wp3", { reviewState: "changes_requested" });
    });
  });

  it("an output awaiting review keeps its Approve", () => {
    listOf({ ...base, id: "wp4", type: "artifact", title: "Cut", isPrimary: true, reviewState: "needs_board_review", metadata: attachmentMeta("video/mp4", "cut.mp4") });
    render(<IssueWorkProductsSection issueId="iss1" />);
    expect(screen.getByText("Output")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("expands a Markdown artifact into its existing review document without a POST", () => {
    listOf({ ...base, id: "wp-md", type: "artifact", title: "Plan", metadata: attachmentMeta("text/markdown", "plan.md") });
    reads["/issues/iss1/documents"] = {
      data: { documents: [{ id: "d1", key: "artifact-review-wp-md", body: "# Launch plan", latestRevisionNumber: 2 }] },
      isLoading: false,
      error: undefined,
    };
    render(<IssueWorkProductsSection issueId="iss1" />);

    expect(screen.queryByText("Output")).not.toBeInTheDocument();
    expect(screen.getByText("Rev 2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /plan/i, expanded: false }));
    expect(screen.getByRole("heading", { name: "Launch plan" })).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("materializes a missing review document via review-document and revalidates documents", async () => {
    post.mockResolvedValue({ id: "d1", key: "artifact-review-wp-md" });
    listOf({ ...base, id: "wp-md", type: "artifact", title: "Plan", metadata: attachmentMeta("text/markdown", "plan.md") });
    render(<IssueWorkProductsSection issueId="iss1" />);

    fireEvent.click(screen.getByRole("button", { name: /plan/i, expanded: false }));
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/issues/iss1/work-products/wp-md/review-document", {});
    });
    await waitFor(() => expect(swrMutate).toHaveBeenCalledWith("/teams/issues/iss1/documents"));
  });

  it("offers Retry when review-document fails, and no Retry when the file is unsupported", async () => {
    post.mockRejectedValueOnce(Object.assign(new Error("boom"), { status: 500 }));
    listOf({ ...base, id: "wp-md", type: "artifact", title: "Plan", metadata: attachmentMeta("text/markdown", "plan.md") });
    render(<IssueWorkProductsSection issueId="iss1" />);
    fireEvent.click(screen.getByRole("button", { name: /plan/i, expanded: false }));
    expect(await screen.findByText("Preview failed to load.")).toBeInTheDocument();

    post.mockRejectedValueOnce(Object.assign(new Error("nope"), { status: 415 }));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/can't be previewed as Markdown/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("shows an error affordance when the list read fails", () => {
    reads[WP_LIST] = { data: undefined, isLoading: false, error: new Error("boom") };
    render(<IssueWorkProductsSection issueId="iss1" />);
    expect(screen.getByText(/couldn't load deliverables/i)).toBeInTheDocument();
  });
});
