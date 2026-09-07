import { describe, it, expect } from "vitest";
import { titleFromDerived } from "../useAgentSessions";

describe("titleFromDerived — a session name a person would recognise", () => {
  it("drops a filler opener and sentence-cases what is left", () => {
    expect(titleFromDerived("can you check the Q3 numbers")).toBe("Check the Q3 numbers");
  });

  it("drops a whole stack of openers, not just the first", () => {
    expect(titleFromDerived("hey, please help me draft the investor update")).toBe(
      "Draft the investor update",
    );
  });

  it("leaves a message that opens with real content alone apart from casing", () => {
    expect(titleFromDerived("draft the investor update")).toBe("Draft the investor update");
    expect(titleFromDerived("Q3 pricing model")).toBe("Q3 pricing model");
  });

  it("truncates on a word boundary rather than mid-word", () => {
    const out = titleFromDerived(
      "summarise the entire quarterly financial performance review document",
    );
    expect(out.length).toBeLessThanOrEqual(41); // 40 + the ellipsis
    expect(out).toMatch(/^Summarise the entire quarterly/);
    expect(out.endsWith("…")).toBe(true);
    // No half-words before the ellipsis.
    expect(out.replace("…", "").trim().split(" ").pop()).not.toMatch(/^(financ|perfor|quarte)$/);
  });

  it("keeps an all-filler message rather than reducing it to nothing", () => {
    expect(titleFromDerived("hey")).toBe("Hey");
    expect(titleFromDerived("can you")).toBe("Can you");
  });

  it("returns empty for empty input", () => {
    expect(titleFromDerived("")).toBe("");
    expect(titleFromDerived("   ")).toBe("");
  });

  it("does not lowercase an acronym or a mid-sentence proper noun", () => {
    expect(titleFromDerived("could you email Sarah about the SOC2 audit")).toBe(
      "Email Sarah about the SOC2 audit",
    );
  });
});
