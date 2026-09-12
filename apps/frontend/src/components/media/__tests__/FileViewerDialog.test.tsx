// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { FileViewerDialog, type FileViewerAttachment } from "../FileViewerDialog";

describe("FileViewerDialog", () => {
  it("renders a PDF through the browser's own renderer via an iframe (no pdf.js)", () => {
    const attachments: FileViewerAttachment[] = [
      { id: "a1", fileName: "report.pdf", contentType: "application/pdf", size: 5000, url: "blob:pdf-1" },
    ];
    render(
      <FileViewerDialog attachments={attachments} index={0} onIndexChange={vi.fn()} onOpenChange={vi.fn()} />,
    );
    const dialog = screen.getByRole("dialog");
    const frame = within(dialog).getByTitle("report.pdf");
    expect(frame.tagName).toBe("IFRAME");
    expect(frame).toHaveAttribute("src", "blob:pdf-1");
  });

  it("falls back to a filename/size card with a download link for a non-previewable type", () => {
    const attachments: FileViewerAttachment[] = [
      { id: "a1", fileName: "archive.zip", contentType: "application/zip", size: 2048, url: "blob:zip-1" },
    ];
    render(
      <FileViewerDialog attachments={attachments} index={0} onIndexChange={vi.fn()} onOpenChange={vi.fn()} />,
    );
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Preview not available for this file type.")).toBeInTheDocument();
    expect(dialog.querySelector("iframe")).toBeNull();
    expect(within(dialog).queryByRole("img")).not.toBeInTheDocument();

    const downloadLink = within(dialog).getByRole("link", { name: /download/i });
    expect(downloadLink).toHaveAttribute("href", "blob:zip-1");
    expect(downloadLink).toHaveAttribute("download", "archive.zip");
  });

  it("preserves the real filename on the download link for an image", () => {
    const attachments: FileViewerAttachment[] = [
      { id: "a1", fileName: "vacation photo.png", contentType: "image/png", size: 1024, url: "blob:img-1" },
    ];
    render(
      <FileViewerDialog attachments={attachments} index={0} onIndexChange={vi.fn()} onOpenChange={vi.fn()} />,
    );
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByAltText("vacation photo.png")).toHaveAttribute("src", "blob:img-1");
    const downloadLink = within(dialog).getByRole("link", { name: /download/i });
    expect(downloadLink).toHaveAttribute("download", "vacation photo.png");
  });

  it("stays closed when the index is out of range", () => {
    const attachments: FileViewerAttachment[] = [
      { id: "a1", fileName: "one.png", contentType: "image/png", url: "blob:one" },
    ];
    render(
      <FileViewerDialog attachments={attachments} index={-1} onIndexChange={vi.fn()} onOpenChange={vi.fn()} />,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
