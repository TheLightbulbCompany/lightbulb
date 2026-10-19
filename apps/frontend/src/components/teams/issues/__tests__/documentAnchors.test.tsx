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
});
