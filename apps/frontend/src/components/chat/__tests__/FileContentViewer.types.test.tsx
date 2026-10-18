// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { utils, write } from "xlsx";
import { FileContentViewer } from "@/components/chat/FileContentViewer";
import type { FileInfo } from "@/hooks/useWorkspaceFiles";

const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

function file(over: Partial<FileInfo>): FileInfo {
  return { name: "f", path: "f", size: 10, modified_at: 0, content: "", binary: false, mime_type: "text/plain", ...over };
}

describe("FileContentViewer previews", () => {
  it("renders csv as a table", async () => {
    render(<FileContentViewer file={file({ name: "a.csv", path: "a.csv", mime_type: "text/csv", content: "a,b\n1,2\n" })} isLoading={false} error={null} />);
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });

  it("renders xlsx (base64) as a table", async () => {
    const wb = utils.book_new();
    utils.book_append_sheet(wb, utils.aoa_to_sheet([["x", "y"], [1, 2]]), "S");
    const bytes = new Uint8Array(write(wb, { type: "array", bookType: "xlsx" }) as ArrayBuffer);
    let bin = "";
    bytes.forEach((b) => (bin += String.fromCharCode(b)));
    render(<FileContentViewer file={file({ name: "a.xlsx", path: "a.xlsx", mime_type: XLSX, binary: true, content: btoa(bin) })} isLoading={false} error={null} />);
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });

  it("keeps html editable (it previews in the right pane, not the tree)", () => {
    const { container } = render(<FileContentViewer file={file({ name: "p.html", path: "p.html", mime_type: "text/html", content: "<p>hi</p>" })} isLoading={false} error={null} />);
    expect(screen.getByLabelText("File contents")).toBeInTheDocument();
    expect(container.querySelector("iframe")).toBeNull();
  });

  it("keeps markdown editable", () => {
    render(<FileContentViewer file={file({ name: "a.md", path: "a.md", mime_type: "text/markdown", content: "# t" })} isLoading={false} error={null} />);
    expect(screen.getByLabelText("File contents")).toBeInTheDocument();
  });
});
