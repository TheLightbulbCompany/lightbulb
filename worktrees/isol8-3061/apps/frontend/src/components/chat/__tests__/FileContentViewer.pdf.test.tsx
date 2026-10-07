// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { FileContentViewer } from "@/components/chat/FileContentViewer";
import type { FileInfo } from "@/hooks/useWorkspaceFiles";

const PDF_BASE64 = btoa("%PDF-1.4 minimal");

function pdfFile(): FileInfo {
  return {
    name: "report.pdf",
    path: "report.pdf",
    size: 1234,
    modified_at: 0,
    content: PDF_BASE64,
    binary: true,
    mime_type: "application/pdf",
  };
}

function opaqueBinaryFile(): FileInfo {
  return {
    name: "data.bin",
    path: "data.bin",
    size: 4,
    modified_at: 0,
    content: null,
    binary: true,
    mime_type: "application/octet-stream",
  };
}

// jsdom doesn't implement object URLs; the component points the browser's
// native PDF renderer at one via <iframe src>.
beforeEach(() => {
  global.URL.createObjectURL = vi.fn(() => "blob:mock-pdf-url");
  global.URL.revokeObjectURL = vi.fn();
});

describe("FileContentViewer PDF preview", () => {
  it("renders a PDF through the browser's native renderer via an iframe (no pdf.js)", () => {
    render(<FileContentViewer file={pdfFile()} isLoading={false} error={null} />);
    const frame = screen.getByTitle("report.pdf");
    expect(frame.tagName).toBe("IFRAME");
    expect(frame).toHaveAttribute("src", "blob:mock-pdf-url");
    expect(global.URL.createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("still falls back to the binary refusal for a non-PDF, non-image binary file", () => {
    render(<FileContentViewer file={opaqueBinaryFile()} isLoading={false} error={null} />);
    expect(screen.getByText("Binary file — preview not available.")).toBeTruthy();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("revokes the previous object URL when switching away from a PDF", () => {
    const { rerender } = render(<FileContentViewer file={pdfFile()} isLoading={false} error={null} />);
    expect(screen.getByTitle("report.pdf")).toBeInTheDocument();
    rerender(<FileContentViewer file={opaqueBinaryFile()} isLoading={false} error={null} />);
    expect(global.URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock-pdf-url");
    expect(screen.queryByTitle("report.pdf")).toBeNull();
  });
});
