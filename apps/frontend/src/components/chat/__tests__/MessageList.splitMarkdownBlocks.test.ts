import { describe, expect, it } from "vitest";
import { splitMarkdownBlocks } from "../MessageList";

describe("splitMarkdownBlocks", () => {
  it("splits paragraphs on blank lines", () => {
    expect(splitMarkdownBlocks("one\n\ntwo\n\nthree")).toEqual(["one", "two", "three"]);
  });

  it("keeps multi-line blocks together", () => {
    expect(splitMarkdownBlocks("# Title\nline two\n\npara")).toEqual(["# Title\nline two", "para"]);
  });

  it("keeps fenced code (with internal blank lines) as one block", () => {
    const md = "intro\n\n```py\na = 1\n\nb = 2\n```\n\noutro";
    expect(splitMarkdownBlocks(md)).toEqual(["intro", "```py\na = 1\n\nb = 2\n```", "outro"]);
  });

  it("keeps an UNTERMINATED fence (still streaming) in the tail block", () => {
    const md = "before\n\n```js\nconst x = 1;\n\nconst y";
    expect(splitMarkdownBlocks(md)).toEqual(["before", "```js\nconst x = 1;\n\nconst y"]);
  });

  it("supports tilde fences", () => {
    const md = "a\n\n~~~\nraw\n\ntext\n~~~";
    expect(splitMarkdownBlocks(md)).toEqual(["a", "~~~\nraw\n\ntext\n~~~"]);
  });

  it("collapses runs of blank lines", () => {
    expect(splitMarkdownBlocks("a\n\n\n\nb")).toEqual(["a", "b"]);
  });

  it("returns the input as a single block when there are no boundaries", () => {
    expect(splitMarkdownBlocks("just one line")).toEqual(["just one line"]);
    expect(splitMarkdownBlocks("")).toEqual([""]);
  });

  it("is prefix-stable while streaming: earlier blocks never change as the tail grows", () => {
    const full = "para one\n\npara two\n\n```js\ncode()\n```\n\ntail text here";
    for (let cut = 1; cut < full.length; cut++) {
      const partial = splitMarkdownBlocks(full.slice(0, cut));
      const complete = splitMarkdownBlocks(full);
      // Every fully-settled partial block (all but the last) must appear
      // verbatim at the same index in the complete split — this is what
      // makes the per-block memo hit while streaming.
      for (let i = 0; i < partial.length - 1; i++) {
        expect(partial[i]).toBe(complete[i]);
      }
    }
  });
});
