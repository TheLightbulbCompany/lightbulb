import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import { FileViewerContent } from "../FileViewerContent";
import { DOCX, DOCX_MAX_BYTES, docxFace, docxHtml } from "../DocxPreview";

const fixture = (name: string) => readFileSync(join(__dirname, "fixtures", name));
const bytes = (name: string) => { const b = fixture(name); return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer; };
const item = (name: string) => ({ fileName: name, contentType: DOCX, url: "blob:x", blob: new Blob([fixture(name)], { type: DOCX }) });

it("a Word file renders its headings, lists and table in the viewer", async () => {
  render(<FileViewerContent item={item("signup.docx")} />);
  expect(await screen.findByRole("heading", { level: 1, name: "Tea tasting sign-up" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { level: 2, name: "What to bring" })).toBeInTheDocument();
  const [bullets, steps] = screen.getAllByRole("list");
  expect(bullets.tagName).toBe("UL");
  expect(within(bullets).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["A mug", "A friend"]);
  expect(steps.tagName).toBe("OL");
  const table = screen.getByRole("table");
  expect(within(table).getAllByRole("row")).toHaveLength(2);
  expect(within(table).getByText("priya@example.com")).toBeInTheDocument();
});

it("a hostile Word file yields no script, handler, javascript: link or outside fetch", async () => {
  const html = await docxHtml(bytes("hostile.docx"));
  const box = document.createElement("div");
  box.innerHTML = html;
  expect(box.querySelectorAll("script, iframe, object, embed, style")).toHaveLength(0);
  expect(html).not.toMatch(/javascript:/i);
  expect([...box.querySelectorAll("*")].flatMap((el) => el.getAttributeNames()).filter((a) => a.startsWith("on"))).toEqual([]);
  expect(box.querySelectorAll("img")).toHaveLength(0);
  expect(html).not.toContain("attacker.example");
  // The script-looking text stays text.
  expect(box.textContent).toContain("<script>alert(1)</script>");
  const link = box.querySelector("a");
  expect(link?.textContent).toBe("click me");
  expect(link?.getAttribute("href")).toBeNull();
});

it("too large or not a docx falls back to Download", async () => {
  const big = new Blob([new Uint8Array(DOCX_MAX_BYTES + 1)], { type: DOCX });
  render(<FileViewerContent item={{ ...item("signup.docx"), fileName: "big.docx", blob: big }} />);
  expect(screen.getByRole("alert")).toHaveTextContent("big.docx is too large to preview. Download it instead.");
  render(<FileViewerContent item={{ ...item("signup.docx"), fileName: "junk.docx", blob: new Blob([new Uint8Array([1, 2, 3])], { type: DOCX }) }} />);
  expect(await screen.findByText(/could not preview junk\.docx/i)).toBeInTheDocument();
});

it("a tile's face is the file's first line and the next few", async () => {
  expect(await docxFace(bytes("signup.docx"))).toEqual({
    heading: "Tea tasting sign-up", lines: ["Saturday, 2 to 4 pm. Add your name below.", "What to bring", "A mug"],
  });
});
