import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { FileViewerContent, isPaneViewable } from "../FileViewerContent";

const XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const blob = (text: string, type: string) => new Blob([text], { type });

it("isPaneViewable names the document-like kinds only", () => {
  expect(isPaneViewable("Text/Markdown; charset=utf-8")).toBe(true);
  expect(["application/pdf", XLSX_TYPE, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/csv", "text/html", "text/markdown", "text/plain"].every(isPaneViewable)).toBe(true);
  expect(["image/png", "video/mp4", "application/zip"].some(isPaneViewable)).toBe(false);
});

it("renders a CSV as a grid with a header row", async () => {
  render(<FileViewerContent item={{ fileName: "vendors.csv", contentType: "text/csv", url: "blob:x", blob: blob("Vendor,Price\nAcme,120\nBirch,99\n", "text/csv") }} />);
  const grid = await screen.findByRole("table");
  expect(within(grid).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Vendor", "Price"]);
  expect(within(grid).getAllByRole("row")).toHaveLength(3);
});

it("rows are bounded", async () => {
  const big = "n\n" + Array.from({ length: 5000 }, (_, i) => String(i)).join("\n");
  render(<FileViewerContent item={{ fileName: "big.csv", contentType: "text/csv", url: "blob:x", blob: blob(big, "text/csv") }} />);
  const grid = await screen.findByRole("table");
  expect(within(grid).getAllByRole("row")).toHaveLength(2001);
  expect(screen.getByText(/showing the first 2,000 of 5,000 rows/i)).toBeInTheDocument();
});

it("unparseable bytes show the fallback card", async () => {
  render(<FileViewerContent item={{ fileName: "x.xlsx", contentType: XLSX_TYPE, url: "blob:x", blob: new Blob([new Uint8Array([0, 1, 2, 3])], { type: XLSX_TYPE }) }} />);
  expect(await screen.findByText(/could not preview/i)).toBeInTheDocument();
});

it("the frame has no origin", async () => {
  render(<FileViewerContent item={{ fileName: "page.html", contentType: "text/html", url: "blob:x", blob: blob("<p>hi</p>", "text/html") }} />);
  const frame = await screen.findByTitle("page.html");
  expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
  expect(frame.getAttribute("src")).toBeNull();
  expect(frame.getAttribute("srcdoc")).toContain("<p>hi</p>");
  expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
});

it("markdown renders and plain text stays monospace", async () => {
  render(<FileViewerContent item={{ fileName: "notes.md", contentType: "text/markdown", url: "blob:x", blob: blob("# Title\n\nbody", "text/markdown") }} />);
  expect(await screen.findByRole("heading", { name: "Title" })).toBeInTheDocument();
  render(<FileViewerContent item={{ fileName: "log.txt", contentType: "text/plain", url: "blob:y", blob: blob("line 1\nline 2", "text/plain") }} />);
  expect((await screen.findByText(/line 1/)).tagName).toBe("PRE");
});

it("a two-sheet workbook shows tabs", async () => {
  const { utils, write } = await import("xlsx");
  const wb = utils.book_new();
  utils.book_append_sheet(wb, utils.aoa_to_sheet([["a"], [1]]), "First");
  utils.book_append_sheet(wb, utils.aoa_to_sheet([["b"], [2]]), "Second");
  const bytes = write(wb, { type: "array", bookType: "xlsx" }) as ArrayBuffer;
  render(<FileViewerContent item={{ fileName: "two.xlsx", contentType: XLSX_TYPE, url: "blob:x", blob: new Blob([bytes], { type: XLSX_TYPE }) }} />);
  await userEvent.click(await screen.findByRole("tab", { name: "Second" }));
  expect(within(screen.getByRole("table")).getAllByRole("columnheader")[0]).toHaveTextContent("b");
});

it("a CSV whose blob type is empty still renders the grid", async () => {
  render(<FileViewerContent item={{ fileName: "v.csv", contentType: "text/csv", url: "blob:x", blob: blob("A,B\n1,2\n", "") }} />);
  expect(within(await screen.findByRole("table")).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["A", "B"]);
});

it("an oversize page shows the too-large card", async () => {
  render(<FileViewerContent item={{ fileName: "huge.html", contentType: "text/html", url: "blob:x", blob: blob("x".repeat(2 * 1024 * 1024 + 1), "text/html") }} />);
  expect(await screen.findByText(/too large to preview/i)).toBeInTheDocument();
});

it("a failing read shows the fallback card, not Loading forever", async () => {
  const spy = vi.spyOn(FileReader.prototype, "readAsText").mockImplementation(function (this: FileReader) {
    queueMicrotask(() => this.onerror?.(new ProgressEvent("error") as ProgressEvent<FileReader>));
  });
  try {
    render(<FileViewerContent item={{ fileName: "a.html", contentType: "text/html", url: "blob:x", blob: blob("<p>x</p>", "text/html") }} />);
    expect(await screen.findByText(/could not preview a\.html/i)).toBeInTheDocument();
    render(<FileViewerContent item={{ fileName: "b.txt", contentType: "text/plain", url: "blob:y", blob: blob("x", "text/plain") }} />);
    expect(await screen.findByText(/could not preview b\.txt/i)).toBeInTheDocument();
  } finally {
    spy.mockRestore();
  }
});

it("columns are bounded", async () => {
  const row = (p: string) => Array.from({ length: 250 }, (_, i) => `${p}${i}`).join(",");
  render(<FileViewerContent item={{ fileName: "wide.csv", contentType: "text/csv", url: "blob:x", blob: blob(`${row("H")}\n${row("v")}\n`, "text/csv") }} />);
  expect(await screen.findByRole("table")).toBeInTheDocument();
  expect(screen.getAllByRole("columnheader")).toHaveLength(200);
  expect(screen.getByText(/showing the first 200 of 250 columns/i)).toBeInTheDocument();
});

it("a long workbook stops parsing at 2,000 rows and still names the real total", async () => {
  const { utils, write } = await import("xlsx");
  const wb = utils.book_new();
  utils.book_append_sheet(wb, utils.aoa_to_sheet([["n"], ...Array.from({ length: 5000 }, (_, i) => [i])]), "S");
  const bytes = write(wb, { type: "array", bookType: "xlsx" }) as ArrayBuffer;
  render(<FileViewerContent item={{ fileName: "long.xlsx", contentType: XLSX_TYPE, url: "blob:x", blob: new Blob([bytes], { type: XLSX_TYPE }) }} />);
  expect(within(await screen.findByRole("table")).getAllByRole("row")).toHaveLength(2001);
  expect(screen.getByText(/showing the first 2,000 of 5,000 rows/i)).toBeInTheDocument();
});
