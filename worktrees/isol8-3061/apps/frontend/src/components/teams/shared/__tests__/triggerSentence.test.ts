import { describe, it, expect } from "vitest";
import { triggerSentence } from "../triggerSentence";

describe("triggerSentence", () => {
  it("schedule", () =>
    expect(triggerSentence({ kind: "schedule", cronExpression: "0 8 * * *" })).toBe(
      "on a schedule — Every day at 08:00",
    ));
  it("webhook", () =>
    expect(triggerSentence({ kind: "webhook" })).toBe("when an app calls this webhook"));
  it("api", () =>
    expect(triggerSentence({ kind: "api" })).toBe("when triggered by the API"));
});
