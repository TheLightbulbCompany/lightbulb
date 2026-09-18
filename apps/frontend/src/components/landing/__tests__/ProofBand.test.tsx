// @vitest-environment jsdom
// jest-dom is normally supplied by tests/setup.ts (the default vitest config).
// Importing it here as well makes this file runnable under the minimal
// vitest.config.unit.ts too; the import is idempotent.
import "@testing-library/jest-dom/vitest";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { ProofBand } from "../ProofBand";
import { COMBINED_ANNUAL_REVENUE } from "../proofBandCopy";

describe("ProofBand", () => {
  it("states the claim as one sentence, with the figure inside it", () => {
    render(<ProofBand />);
    // Asserted on the flattened text rather than per-element, because the
    // figure is a <b> inside the sentence: the claim only reads correctly if
    // the surrounding words and the number share one line of prose.
    expect(screen.getByRole("paragraph")).toHaveTextContent(
      `Teams that communicate through Lightbulb do $${COMBINED_ANNUAL_REVENUE} in combined annual revenue.`,
    );
  });

  it("keeps the figure in one constant so bumping it is a one-line edit", () => {
    // The owner bumps this as the number grows (2M+ -> 3M+). If a second copy
    // ever appears in the markup this fails, which is the point.
    const { container } = render(<ProofBand />);
    const occurrences = (container.textContent ?? "").split(COMBINED_ANNUAL_REVENUE).length - 1;
    expect(occurrences).toBe(1);
  });

  it("renders the dollar sign as markup, not as part of the figure constant", () => {
    // The constant is the amount alone so it reads as a number everywhere it
    // is reused; the unit is presentation and belongs in the component.
    expect(COMBINED_ANNUAL_REVENUE).not.toContain("$");
    render(<ProofBand />);
    expect(screen.getByRole("paragraph").textContent).toContain(`$${COMBINED_ANNUAL_REVENUE}`);
  });

  it("does not use the .lb-brk hard break", () => {
    // lb-brk is display:none below 900px. Every other landing headline pairs
    // it with JSX-trimmed whitespace and therefore concatenates two words on
    // mobile (see LandingHeadlines.spacing.test.tsx). This band wraps on
    // max-width instead so it cannot inherit that bug.
    const { container } = render(<ProofBand />);
    expect(container.querySelector(".lb-brk")).toBeNull();
  });
});
