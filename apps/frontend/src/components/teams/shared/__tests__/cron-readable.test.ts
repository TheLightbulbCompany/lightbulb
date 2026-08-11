import { describe, it, expect } from "vitest";
import { describeCron } from "../cron-readable";

describe("describeCron", () => {
  it("daily", () => expect(describeCron("0 8 * * *")).toBe("Every day at 08:00"));
  it("weekday", () => expect(describeCron("0 8 * * 1")).toBe("Every Monday at 08:00"));
  it("hourly", () => expect(describeCron("0 * * * *")).toBe("Every hour"));
  it("weekdays", () =>
    expect(describeCron("0 7 * * 1-5")).toBe("Every weekday at 07:00"));
  it("unparseable falls back", () =>
    expect(describeCron("*/13 3 5 6 2")).toBe("*/13 3 5 6 2"));
});
