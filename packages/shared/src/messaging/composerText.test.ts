import { describe, expect, it } from "vitest";
import { wrapSelection } from "./composerText";

describe("wrapSelection", () => {
  it("wraps and unwraps the inline pairs", () => {
    expect(wrapSelection("ship it", 0, 4, "bold")).toEqual({ text: "**ship** it", selectionStart: 2, selectionEnd: 6 });
    expect(wrapSelection("**ship** it", 2, 6, "bold")).toEqual({ text: "ship it", selectionStart: 0, selectionEnd: 4 });
    expect(wrapSelection("ship", 0, 4, "italic").text).toBe("_ship_");
    expect(wrapSelection("ship", 0, 4, "strike").text).toBe("~~ship~~");
    expect(wrapSelection("ship", 0, 4, "code").text).toBe("`ship`");
  });
  it("fences a code block and selects its body", () => {
    expect(wrapSelection("ship", 0, 4, "codeblock")).toEqual({ text: "```\nship\n```", selectionStart: 4, selectionEnd: 8 });
  });
  it("prefixes every line of the touched block for lists", () => {
    expect(wrapSelection("one\ntwo", 0, 7, "ul").text).toBe("- one\n- two");
    expect(wrapSelection("one\ntwo", 0, 7, "ol").text).toBe("1. one\n2. two");
  });
  it("builds a link and selects the url placeholder", () => {
    expect(wrapSelection("docs", 0, 4, "link")).toEqual({ text: "[docs](url)", selectionStart: 7, selectionEnd: 10 });
    expect(wrapSelection("", 0, 0, "link").text).toBe("[text](url)");
  });
  it("does nothing to an empty selection beyond inserting bare markers", () => {
    expect(wrapSelection("", 0, 0, "bold")).toEqual({ text: "****", selectionStart: 2, selectionEnd: 2 });
  });
  it("toggles a selection that is already wrapped back off", () => {
    const wrapped = wrapSelection("ship", 0, 4, "code");
    expect(wrapped.text).toBe("`ship`");
    const unwrapped = wrapSelection(wrapped.text, wrapped.selectionStart, wrapped.selectionEnd, "code");
    expect(unwrapped).toEqual({ text: "ship", selectionStart: 0, selectionEnd: 4 });
  });
  it("wraps a selection that spans a line break without treating it as a list", () => {
    expect(wrapSelection("one\ntwo", 0, 7, "bold")).toEqual({ text: "**one\ntwo**", selectionStart: 2, selectionEnd: 9 });
  });
  it("prefixes a list block that spans a line break for every line in range", () => {
    expect(wrapSelection("one\ntwo\nthree", 0, 7, "ul").text).toBe("- one\n- two\nthree");
  });
  it("handles a caret at position 0 with nothing selected", () => {
    // An empty selection still contributes value.slice(end) = the whole
    // string (start === end === 0), so both markers land before all of it.
    expect(wrapSelection("hello", 0, 0, "bold")).toEqual({ text: "****hello", selectionStart: 2, selectionEnd: 2 });
  });
  it("handles a caret at end-of-text with nothing selected", () => {
    const value = "hello";
    expect(wrapSelection(value, value.length, value.length, "italic")).toEqual({ text: "hello__", selectionStart: 6, selectionEnd: 6 });
  });
});
