// Ported from Paperclip ui/src/components/issue-output/IssueOutputSection.test.tsx
// @ v2026.916.1-isol8.2, adapted: media/Open/Download go through the Teams BFF
// as blobs, so URLs are asserted on the download calls, not on the markup.
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { IssueWorkProduct } from "@/components/teams/shared/types";

const download = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ download }),
}));

import { IssueOutputSection as Section } from "../issue-output/IssueOutputSection";
import { AttachmentIssueContext } from "@/components/teams/shared/lib/attachments";

// The issue page provides the owning issue; attachment bytes are issue-scoped.
function IssueOutputSection(props: React.ComponentProps<typeof Section>) {
  return (
    <AttachmentIssueContext.Provider value="issue-1">
      <Section {...props} />
    </AttachmentIssueContext.Provider>
  );
}

function makeWorkProduct(overrides: Partial<IssueWorkProduct> & { id: string }): IssueWorkProduct {
  return {
    issueId: "issue-1",
    type: "artifact",
    provider: "paperclip",
    title: "output",
    url: null,
    status: "active",
    reviewState: "none",
    isPrimary: false,
    metadata: null,
    createdAt: "2026-05-30T12:00:00Z",
    updatedAt: "2026-05-30T12:00:00Z",
    ...overrides,
  };
}

const UUIDS: Record<string, string> = {
  "att-1": "11111111-1111-4111-8111-111111111111",
  "att-pdf": "33333333-3333-4333-8333-333333333333",
  "att-img": "44444444-4444-4444-8444-444444444444",
  "att-webm": "55555555-5555-4555-8555-555555555555",
};

function metadata(key: string, contentType: string, filename: string) {
  const attachmentId = UUIDS[key] ?? key;
  return {
    attachmentId,
    contentType,
    byteSize: 19_293_798,
    contentPath: `/api/attachments/${attachmentId}/content`,
    openPath: `/api/attachments/${attachmentId}/content`,
    downloadPath: `/api/attachments/${attachmentId}/content?download=1`,
    originalFilename: filename,
  };
}

beforeEach(() => {
  download.mockReset();
  download.mockResolvedValue(new Blob(["x"], { type: "video/mp4" }));
  URL.createObjectURL = vi.fn(() => "blob:mock");
  URL.revokeObjectURL = vi.fn();
});

describe("IssueOutputSection", () => {
  it("renders a playable, downloadable video as the primary output", async () => {
    const { container } = render(
      <IssueOutputSection
        workProducts={[
          makeWorkProduct({
            id: "wp-1",
            title: "Demo walkthrough",
            isPrimary: true,
            metadata: metadata("att-1", "video/mp4", "demo.mp4"),
          }),
        ]}
      />,
    );

    // Bytes fetched through the BFF (never the raw upstream path) into a player.
    await waitFor(() => expect(container.querySelector("video[controls]")).not.toBeNull());
    expect(download).toHaveBeenCalledWith(`/issues/issue-1/attachments/${UUIDS["att-1"]}/content`);
    expect(container.querySelector("video")?.getAttribute("src")).toBe("blob:mock");
    expect(screen.getByText("demo.mp4")).toBeInTheDocument();
    expect(screen.getByText("Output")).toBeInTheDocument();
    expect(screen.getByText("18.4 MB")).toBeInTheDocument();
    expect(screen.getByText("Primary")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open" })).toBeInTheDocument();

    download.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Download demo.mp4" }));
    await waitFor(() =>
      expect(download).toHaveBeenCalledWith(`/issues/issue-1/attachments/${UUIDS["att-1"]}/content`),
    );
  });

  it("renders nothing when the issue has no artifact outputs (empty state)", () => {
    const { container } = render(
      <IssueOutputSection workProducts={[makeWorkProduct({ id: "pr-1", type: "pull_request" })]} />,
    );
    expect(container).toBeEmptyDOMElement();
    const empty = render(<IssueOutputSection workProducts={[]} />);
    expect(empty.container).toBeEmptyDOMElement();
  });

  it("renders nothing for markdown-only artifact work products", () => {
    const { container } = render(
      <IssueOutputSection
        workProducts={[
          makeWorkProduct({
            id: "wp-markdown",
            title: "plan.md",
            isPrimary: true,
            metadata: metadata("att-1", "text/markdown", "plan.md"),
          }),
        ]}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the primary card plus an Also produced list for multiple outputs", () => {
    render(
      <IssueOutputSection
        workProducts={[
          makeWorkProduct({
            id: "wp-primary",
            isPrimary: true,
            createdAt: "2026-05-30T12:00:00Z",
            metadata: metadata("att-1", "video/mp4", "summary.mp4"),
          }),
          makeWorkProduct({
            id: "wp-pdf",
            createdAt: "2026-05-30T11:00:00Z",
            metadata: metadata("att-pdf", "application/pdf", "talking-points.pdf"),
          }),
        ]}
      />,
    );

    expect(screen.getByText("Also produced")).toBeInTheDocument();
    expect(screen.getByText("summary.mp4")).toBeInTheDocument();
    expect(screen.getByText("talking-points.pdf")).toBeInTheDocument();
    // PDF glyph tile label appears for the secondary row
    expect(screen.getByText("PDF")).toBeInTheDocument();
  });

  it("renders secondary image and video outputs as preview tiles", async () => {
    render(
      <IssueOutputSection
        workProducts={[
          makeWorkProduct({
            id: "wp-primary",
            isPrimary: true,
            metadata: metadata("att-pdf", "application/pdf", "brief.pdf"),
          }),
          makeWorkProduct({
            id: "wp-image",
            createdAt: "2026-05-30T11:00:00Z",
            metadata: metadata("att-img", "image/png", "screenshot.png"),
          }),
          makeWorkProduct({
            id: "wp-video",
            createdAt: "2026-05-30T10:00:00Z",
            metadata: metadata("att-webm", "video/webm", "clip.webm"),
          }),
        ]}
      />,
    );

    expect(screen.getByText("Also produced")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open screenshot.png" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open clip.webm" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByAltText("screenshot.png")).toBeInTheDocument());
    // The PDF primary is a file tile, never prefetched; only the two media tiles are.
    expect(download).toHaveBeenCalledTimes(2);
  });

  it("surfaces an output with failed/invalid attachment metadata without crashing", () => {
    const { container } = render(
      <IssueOutputSection
        workProducts={[
          makeWorkProduct({
            id: "wp-broken",
            title: "broken-output.mp4",
            isPrimary: true,
            // Missing required path fields → fails the shared metadata rules
            metadata: { attachmentId: "att-x", contentType: "video/mp4" },
          }),
        ]}
      />,
    );

    expect(screen.getByText("broken-output.mp4")).toBeInTheDocument();
    expect(screen.getByText(/metadata is unavailable/)).toBeInTheDocument();
    // No video element and no download action can be built from invalid metadata
    expect(container.querySelector("video")).toBeNull();
    expect(screen.queryByRole("button", { name: /download/i })).not.toBeInTheDocument();
    expect(download).not.toHaveBeenCalled();
  });
});
