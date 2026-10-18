import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";

import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { computeAnchor } from "../lib/documentAnnotationSelection";
import { verifyDocumentAnchorSelector } from "../lib/documentAnchors";

// A body MDXEditor re-serialised (escapes, `***` rule, pipe tables). The
// server 422'd passage comments on it because our selector came from a
// different projection than its verifier's.
const markdown = readFileSync(join(__dirname, "fixtures/escaped-doc-fixture.md"), "utf8");

function anchorFor(quote: string, nth = 0, body = markdown) {
  const { container, unmount } = render(<MarkdownView>{body}</MarkdownView>);
  const containerText = container.textContent ?? "";
  unmount();
  let start = -1;
  for (let i = 0; i <= nth; i += 1) start = containerText.indexOf(quote, start + 1);
  expect(start).toBeGreaterThanOrEqual(0);
  return computeAnchor({ markdown: body, containerText, startOffset: start, endOffset: start + quote.length });
}

/** The raw markdown the anchor pins, after checking the server would accept it. */
function pinned(quote: string, nth = 0, body = markdown) {
  const anchor = anchorFor(quote, nth, body);
  expect(anchor).not.toBeNull();
  expect(verifyDocumentAnchorSelector({ markdown: body, selector: anchor!.selector }).reason).toBe("verified");
  const { markdownStart, markdownEnd } = anchor!.selector.position;
  return { start: markdownStart, text: body.slice(markdownStart, markdownEnd) };
}

describe("computeAnchor on an escaped (MDXEditor-saved) document", () => {
  it.each([
    ["The following items require the owner (Mira Track) to supply:", "The following items require the owner (Mira Track) to supply:"],
    // After the `\=`, `\[` and `\#` escapes; `\[the plan]` renders its brackets.
    ["see [the plan] and item #2", "see \\[the plan] and item \\#2"],
    ["Recommended: EIN Presswire ~$49", "Recommended: EIN Presswire \\~$49"],
  ])("pins the exact source span the server accepts (case %#)", (quote, source) => {
    expect(pinned(quote).text).toBe(source);
  });

  it("pins each occurrence of a repeated quote to its own span", () => {
    expect(pinned("paid", 0)).toEqual({ start: markdown.indexOf("all paid") + 4, text: "paid" });
    expect(pinned("paid", 1)).toEqual({ start: markdown.indexOf("traffic paid") + 8, text: "paid" });
  });
});

describe("computeAnchor never pins text the page does not render", () => {
  it("skips an image's alt text", () => {
    const body = "![revenue](https://x/a.png)\n\nrevenue up. revenue down.";
    expect(pinned("revenue", 0, body).start).toBe(body.indexOf("revenue up"));
    expect(pinned("revenue", 1, body).start).toBe(body.indexOf("revenue down"));
  });

  it("skips a reference-link definition's URL", () => {
    const body = "[plan][1] and plan\n\n[1]: https://plan.example";
    expect(pinned("plan", 0, body).start).toBe(1);
    expect(pinned("plan", 1, body).start).toBe(body.indexOf("and plan") + 4);
  });

  it("skips image alt text whose words match the visible sentence's context", () => {
    const body = "Intro text.\n\n![Sales grew in March](x)\n\nSales grew in March.";
    expect(pinned("grew", 0, body)).toEqual({ start: body.lastIndexOf("grew"), text: "grew" }); // 46
  });

  it("skips a reference definition whose title repeats a visible sentence", () => {
    const body = "Intro text.\n\n[x]: https://x 'Sales grew in March'\n\nSales grew in March.";
    expect(pinned("grew", 0, body).start).toBe(body.lastIndexOf("grew"));
  });

  it("skips the source of a card fence that repeats a visible sentence", () => {
    // The card renders as a card, not as its source text.
    const body = "Intro.\n\n```openui-lang\nIntro. Revenue is up. Done\n```\n\nRevenue is up. Done";
    const containerText = "Intro.\n\nRevenue is up. Done";
    const start = containerText.indexOf("Revenue is up.");
    const anchor = computeAnchor({ markdown: body, containerText, startOffset: start, endOffset: start + "Revenue is up.".length });
    expect(anchor).not.toBeNull();
    expect(verifyDocumentAnchorSelector({ markdown: body, selector: anchor!.selector }).reason).toBe("verified");
    expect(anchor!.selector.position.markdownStart).toBe(body.lastIndexOf("Revenue is up."));
  });

  it("keeps inline code that holds a tag: the code renders", () => {
    const body = "Set the `<main>` tag first.\n\nSet the main tag first.";
    expect(pinned("main", 0, body)).toEqual({ start: body.indexOf("main"), text: "main" });
  });

  it("keeps an autolink: it renders", () => {
    const body = "Docs at <https://docs.example> today.\n\nDocs at docs.example today.";
    expect(pinned("docs.example", 0, body)).toEqual({ start: body.indexOf("docs.example"), text: "docs.example" });
  });

  it("keeps a footnote definition: it renders", () => {
    const body = "Revenue grew.[^1]\n\nRevenue grew.\n\n[^1]: Revenue grew per report.";
    expect(pinned("Revenue grew per report", 0, body)).toEqual({
      start: body.lastIndexOf("Revenue grew per report"),
      text: "Revenue grew per report",
    });
  });

  it("keeps an image or card fence that sits inside a code block", () => {
    const body = "````markdown\n```mermaid\nflow A\n```\n````\n\nflow A";
    const containerText = "```mermaid\nflow A\n```\nflow A";
    const start = containerText.indexOf("flow A");
    const anchor = computeAnchor({ markdown: body, containerText, startOffset: start, endOffset: start + 6 });
    expect(anchor!.selector.position.markdownStart).toBe(body.indexOf("flow A"));
  });

  it("still anchors when the filter flags the only occurrence", () => {
    // The definition-shaped line is the only "Plan B"; the filter must not hide the Comment button.
    const body = "Intro.\n\n[Plan B]: is the fallback";
    const containerText = "Intro.\n[Plan B]: is the fallback";
    const start = containerText.indexOf("Plan B");
    const anchor = computeAnchor({ markdown: body, containerText, startOffset: start, endOffset: start + 6 });
    expect(anchor).not.toBeNull();
    expect(verifyDocumentAnchorSelector({ markdown: body, selector: anchor!.selector }).reason).toBe("verified");
    expect(body.slice(anchor!.selector.position.markdownStart, anchor!.selector.position.markdownEnd)).toBe("Plan B");
  });

  it("refuses a quote over the server's 10,000-character cap", () => {
    const body = "word ".repeat(2500).trim();
    expect(computeAnchor({ markdown: body, containerText: body, startOffset: 0, endOffset: body.length })).toBeNull();
  });
});
