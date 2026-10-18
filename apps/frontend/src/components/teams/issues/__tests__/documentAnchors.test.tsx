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

function renderedText(): string {
  const { container } = render(<MarkdownView>{markdown}</MarkdownView>);
  return container.textContent ?? "";
}

function anchorFor(quote: string) {
  const containerText = renderedText();
  const start = containerText.indexOf(quote);
  expect(start).toBeGreaterThanOrEqual(0);
  return computeAnchor({ markdown, containerText, startOffset: start, endOffset: start + quote.length });
}

describe("computeAnchor on an escaped (MDXEditor-saved) document", () => {
  it.each([
    "The following items require the owner (Mira Track) to supply:",
    // After the `\=`, `\[` and `\#` escapes; `\[the plan]` renders its brackets.
    "see [the plan] and item #2",
    "Recommended: EIN Presswire ~$49",
  ])("builds a selector the server's verifier accepts (case %#)", (quote) => {
    const anchor = anchorFor(quote);
    expect(anchor).not.toBeNull();
    const result = verifyDocumentAnchorSelector({ markdown, selector: anchor!.selector });
    expect(result.reason).toBe("verified");
    const { markdownStart, markdownEnd } = anchor!.selector.position;
    expect(markdown.slice(markdownStart, markdownEnd)).toContain(quote.split(" ")[0]);
  });
});
