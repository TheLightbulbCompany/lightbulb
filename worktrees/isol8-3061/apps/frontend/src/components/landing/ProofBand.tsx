import { COMBINED_ANNUAL_REVENUE } from "./proofBandCopy";

/**
 * Social proof, as one sentence.
 *
 * Sits between the hero and "how it works" rather than next to pricing, which
 * was the first instinct: the figure borrows `.lb-price-hero`'s Lora-with-a
 * -superscript-unit idiom so it reads as native, and putting it directly above
 * the `$50/mo` price made two large centred dollar figures compete two
 * sections apart. Up here it is the first thing after the pitch and it is
 * nowhere near the price.
 *
 * Deliberately does NOT use `.lb-brk`. Every other landing headline pairs that
 * hard break with JSX-trimmed whitespace, and since the class is
 * `display: none` below 900px those headlines concatenate two words on mobile
 * (fixed alongside this; see `LandingHeadlines.spacing.test.tsx`). This band
 * wraps on `max-width` instead, so it cannot inherit the bug.
 */
export function ProofBand() {
  return (
    <section className="lb-proof" id="proof">
      <div className="lb-wrap">
        <p className="lb-proof-line">
          Teams that communicate through Lightbulb do{" "}
          <b>
            <sup>$</sup>
            {COMBINED_ANNUAL_REVENUE}
          </b>{" "}
          in combined annual revenue.
        </p>
      </div>
    </section>
  );
}
