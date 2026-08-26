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
  for (const p of posts) {
    expect(p.title && p.description && p.author).toBeTruthy();
    expect(p.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(Array.isArray(p.tags)).toBe(true);
    // The page renders its own <h1>; a body-level H1 would duplicate it.
    expect(p.content.startsWith("# ")).toBe(false);
  }
  const docs = getAllDocs();
  expect(docs.map((d) => d.order)).toEqual([1, 2, 3, 4, 5]);
  for (const d of docs) expect(d.title && d.description).toBeTruthy();
});
