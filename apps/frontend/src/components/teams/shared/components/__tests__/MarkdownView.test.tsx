import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const download = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ download }),
}));

import { MarkdownView, attachmentPathFromHref } from "../MarkdownView";

describe("attachmentPathFromHref", () => {
  it("maps upstream contentPath hrefs to the BFF download path", () => {
    expect(attachmentPathFromHref("/api/attachments/att-1/content")).toBe(
      "/attachments/att-1/content",
    );
    expect(attachmentPathFromHref("/api/attachments/att-1/content?download=1")).toBe(
      "/attachments/att-1/content",
    );
  });

  it("rejects everything else", () => {
    expect(attachmentPathFromHref(undefined)).toBeNull();
    expect(attachmentPathFromHref("https://example.com/api/attachments/x/content")).toBeNull();
    expect(attachmentPathFromHref("/api/attachments/att-1/other")).toBeNull();
    expect(attachmentPathFromHref("/ACME/issues/ISO-1")).toBeNull();
  });
});

describe("MarkdownView attachment links", () => {
  beforeEach(() => {
    download.mockReset().mockResolvedValue(new Blob(["hi"]));
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
  });

  it("renders an agent-pasted attachment link as an authenticated download", async () => {
    render(
      <MarkdownView>
        {"- Artifact: [ICP rewrite](/api/attachments/att-9/content)"}
      </MarkdownView>,
    );
    const button = screen.getByRole("button", { name: "ICP rewrite" });
    fireEvent.click(button);
    await waitFor(() => expect(download).toHaveBeenCalledWith("/attachments/att-9/content"));
  });

  it("leaves ordinary links as plain anchors", () => {
    render(<MarkdownView>{"[docs](https://example.com/docs)"}</MarkdownView>);
    const anchor = screen.getByRole("link", { name: "docs" });
    expect(anchor).toHaveAttribute("href", "https://example.com/docs");
  });
});
