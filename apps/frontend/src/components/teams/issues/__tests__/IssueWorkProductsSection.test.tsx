import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { SWRResponse } from "swr";

// Per-path read stub + a shared patch spy: the section reads the work-products
// list and PATCHes the review state through useTeamsApi.
let reads: Record<string, Partial<SWRResponse<unknown, Error>>> = {};
const patch = vi.fn();
const swrMutate = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) =>
      reads[path] ?? { data: undefined, isLoading: false, error: undefined },
    patch,
  }),
}));
vi.mock("swr", async (orig) => ({
  ...(await orig<object>()),
  useSWRConfig: () => ({ mutate: swrMutate }),
}));

import { IssueWorkProductsSection } from "../IssueWorkProductsSection";

beforeEach(() => {
  reads = {};
  patch.mockReset();
  swrMutate.mockReset();
});

describe("IssueWorkProductsSection", () => {
  it("renders nothing when there are no work products (no error, not loading)", () => {
    reads["/issues/iss1/work-products"] = {
      data: { workProducts: [] },
      isLoading: false,
      error: undefined,
    };
    const { container } = render(<IssueWorkProductsSection issueId="iss1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the Output section with a deliverable + its Open link", () => {
    reads["/issues/iss1/work-products"] = {
      data: {
        workProducts: [
          {
            id: "wp1",
            issueId: "iss1",
            type: "preview_url",
            provider: "paperclip",
            title: "Deploy preview",
            url: "https://preview.example.com",
            status: "ready_for_review",
            reviewState: "approved",
            isPrimary: true,
            createdAt: "2026-01-01T00:00:00Z",
          },
        ],
      },
      isLoading: false,
      error: undefined,
    };
    render(<IssueWorkProductsSection issueId="iss1" />);

    expect(screen.getByText("Output")).toBeInTheDocument();
    expect(screen.getByText("Deploy preview")).toBeInTheDocument();
    // Type + review-state pills.
    expect(screen.getByText("Preview Url")).toBeInTheDocument();
    expect(screen.getByText("Approved")).toBeInTheDocument();
    // Open link points at the deliverable url.
    expect(screen.getByRole("link", { name: /open deploy preview/i })).toHaveAttribute(
      "href",
      "https://preview.example.com",
    );
    // Already-approved products expose no review controls.
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it.each([
    ["data:", "data:text/html,<script>alert(1)</script>"],
    ["javascript:", "javascript:alert(1)"],
  ])("does not render a live Open link for an unsafe %s url", (_label, url) => {
    reads["/issues/iss1/work-products"] = {
      data: {
        workProducts: [
          {
            id: "wp-unsafe",
            issueId: "iss1",
            type: "preview_url",
            provider: "paperclip",
            title: "Sketchy preview",
            url,
            status: "ready_for_review",
            reviewState: "none",
            createdAt: "2026-01-01T00:00:00Z",
          },
        ],
      },
      isLoading: false,
      error: undefined,
    };
    render(<IssueWorkProductsSection issueId="iss1" />);

    // The row still surfaces (title, type pill) — only the Open affordance drops.
    expect(screen.getByText("Sketchy preview")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /open sketchy preview/i })).not.toBeInTheDocument();
    // Nothing renders the raw unsafe url as a live href.
    expect(document.querySelector(`a[href="${url}"]`)).toBeNull();
  });

  it("Approve PATCHes reviewState=approved and revalidates the list", async () => {
    patch.mockResolvedValue({});
    reads["/issues/iss1/work-products"] = {
      data: {
        workProducts: [
          {
            id: "wp2",
            issueId: "iss1",
            type: "artifact",
            provider: "paperclip",
            title: "Report.pdf",
            url: null,
            status: "ready_for_review",
            reviewState: "needs_board_review",
            createdAt: "2026-01-02T00:00:00Z",
          },
        ],
      },
      isLoading: false,
      error: undefined,
    };
    render(<IssueWorkProductsSection issueId="iss1" />);

    // A needs-review product surfaces the review pill + Approve / Request-changes.
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(patch).toHaveBeenCalledWith("/issues/iss1/work-products/wp2", {
        reviewState: "approved",
      });
    });
    expect(swrMutate).toHaveBeenCalledWith("/teams/issues/iss1/work-products");
  });

  it("Request changes PATCHes reviewState=changes_requested", async () => {
    patch.mockResolvedValue({});
    reads["/issues/iss1/work-products"] = {
      data: {
        workProducts: [
          {
            id: "wp3",
            issueId: "iss1",
            type: "pull_request",
            provider: "github",
            title: "Fix login",
            url: "https://github.com/x/y/pull/1",
            status: "ready_for_review",
            reviewState: "needs_board_review",
            createdAt: "2026-01-03T00:00:00Z",
          },
        ],
      },
      isLoading: false,
      error: undefined,
    };
    render(<IssueWorkProductsSection issueId="iss1" />);

    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
    await waitFor(() => {
      expect(patch).toHaveBeenCalledWith("/issues/iss1/work-products/wp3", {
        reviewState: "changes_requested",
      });
    });
  });

  it("shows an error affordance when the list read fails", () => {
    reads["/issues/iss1/work-products"] = {
      data: undefined,
      isLoading: false,
      error: new Error("boom"),
    };
    render(<IssueWorkProductsSection issueId="iss1" />);
    expect(screen.getByText(/couldn't load deliverables/i)).toBeInTheDocument();
  });
});
