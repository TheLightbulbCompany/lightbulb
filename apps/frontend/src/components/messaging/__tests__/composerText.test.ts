import { describe, expect, it } from "vitest";
import { activeTrigger, applyCompletion } from "@lightbulb/shared";
import { wrapSelection } from "../composerText";

describe("activeTrigger", () => {
  it("opens a mention trigger at the start of the field and after whitespace", () => {
    expect(activeTrigger("@", 1)).toEqual({ kind: "mention", start: 0, end: 1, query: "" });
    expect(activeTrigger("Ask @fri", 8)).toEqual({ kind: "mention", start: 4, end: 8, query: "fri" });
  });
  it("carries up to two internal spaces so a display name is reachable", () => {
    expect(activeTrigger("Hi @Aden Kim", 12)).toEqual({ kind: "mention", start: 3, end: 12, query: "Aden Kim" });
    expect(activeTrigger("Hi @Aden Kim the third", 22)).toBeNull();
  });
  it("ignores an @ glued to a word and a caret before the sigil", () => {
    expect(activeTrigger("mail@example", 12)).toBeNull();
    expect(activeTrigger("Ask @fri", 3)).toBeNull();
    expect(activeTrigger("Ask @fri", null)).toBeNull();
  });
  it("opens an emoji trigger only after two characters", () => {
    expect(activeTrigger("nice :t", 7)).toBeNull();
    expect(activeTrigger("nice :ta", 8)).toEqual({ kind: "emoji", start: 5, end: 8, query: "ta" });
    expect(activeTrigger("nice :tada", 10)).toEqual({ kind: "emoji", start: 5, end: 10, query: "tada" });
  });
});

describe("applyCompletion", () => {
  it("replaces the trigger and leaves the caret after a single trailing space", () => {
    const trigger = activeTrigger("Ask @fri", 8)!;
    expect(applyCompletion("Ask @fri", trigger, "@friday")).toEqual({ text: "Ask @friday ", caret: 12 });
  });
  it("does not double the space when text already follows", () => {
    const trigger = activeTrigger("Ask @fri", 8)!;
    expect(applyCompletion("Ask @fri now", trigger, "@friday")).toEqual({ text: "Ask @friday now", caret: 11 });
  });
});

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
});
