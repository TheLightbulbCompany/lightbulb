import { describe, expect, it } from "vitest";
import { parseFrontmatter, readingMinutes } from "../markdown";
import { getAllPosts } from "../blog";
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

it("readingMinutes never drops below 1", () => {
  expect(readingMinutes("one two")).toBe(1);
  expect(readingMinutes(Array(660).fill("w").join(" "))).toBe(3);
});

it("every post and doc on disk has the fields the pages render", () => {
  const posts = getAllPosts();
  expect(posts.length).toBeGreaterThan(0);
  const nonEmptyString = (v: unknown) => typeof v === "string" && v.trim().length > 0;
  for (const p of posts) {
    expect([p.title, p.description, p.author].every(nonEmptyString)).toBe(true);
    expect(p.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(Array.isArray(p.tags) && p.tags.every((t) => typeof t === "string")).toBe(true);
    // The page renders its own <h1> from `title`, so a body must not OPEN with
    // its title as an H1 (the duplicate-heading bug this guards). Deliberately
    // not scanning every line: `# comment` inside a fenced shell block is valid.
    expect(p.content).not.toMatch(/^(?:\r?\n)* {0,3}#(?:[ \t]|$)/);
  }
  const docs = getAllDocs();
  expect(docs.map((d) => d.order)).toEqual([1, 2, 3, 4, 5]);
  for (const d of docs) expect([d.title, d.description].every(nonEmptyString)).toBe(true);
});
