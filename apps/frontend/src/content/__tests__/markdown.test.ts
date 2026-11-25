import { describe, expect, it } from "vitest";
import { parseFrontmatter } from "../markdown";
import { getAllDocs } from "../docs";

describe("parseFrontmatter", () => {
  it("parses JSON values, falls back to bare strings, and returns the body", () => {
    const { meta, content } = parseFrontmatter('---\ntitle: "A: b"\ntags: ["x","y"]\norder: 2\nauthor: isol8\n---\n\nbody\n');
    expect(meta).toEqual({ title: "A: b", tags: ["x", "y"], order: 2, author: "isol8" });
    expect(content).toBe("body\n");
  });
  it("accepts CRLF line endings", () => {
    const { meta, content } = parseFrontmatter('---\r\ntitle: "A"\r\norder: 1\r\n---\r\n\r\nbody\r\n');
    expect(meta).toEqual({ title: "A", order: 1 });
    expect(content).toBe("body\r\n");
  });
  it("treats a file without frontmatter as all body", () => {
    expect(parseFrontmatter("# hi\n")).toEqual({ meta: {}, content: "# hi\n" });
  });
});

it("every doc on disk has the fields the pages render", () => {
  const nonEmptyString = (v: unknown) => typeof v === "string" && v.trim().length > 0;
  const docs = getAllDocs();
  expect(docs.map((d) => d.order)).toEqual(docs.map((_, i) => i + 1));
  for (const d of docs) expect([d.title, d.description].every(nonEmptyString)).toBe(true);
});
