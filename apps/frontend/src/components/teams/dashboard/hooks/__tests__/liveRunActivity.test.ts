import { describe, it, expect } from "vitest";
import {
  liveRunActivity,
  liveRunTitle,
  formatSilenceAgeMs,
  type LiveRun,
} from "../useLiveRuns";

function run(overrides: Partial<LiveRun>): LiveRun {
  return { id: "r1", status: "running", ...overrides };
}

describe("liveRunActivity", () => {
  it("treats a run with no silence signal (or ok/snoozed) as running", () => {
    expect(liveRunActivity(run({}))).toBe("running");
    expect(liveRunActivity(run({ outputSilence: { level: "ok" } }))).toBe("running");
    expect(liveRunActivity(run({ outputSilence: { level: "snoozed" } }))).toBe("running");
    expect(liveRunActivity(run({ outputSilence: { level: "not_applicable" } }))).toBe(
      "running",
    );
  });

  it("classifies suspicious + critical output-silence as stalled (mirrors upstream watchdog)", () => {
    expect(liveRunActivity(run({ outputSilence: { level: "suspicious" } }))).toBe("stalled");
    expect(liveRunActivity(run({ outputSilence: { level: "critical" } }))).toBe("stalled");
  });
});

describe("liveRunTitle", () => {
  it("describes a healthy run, preferring nextAction", () => {
    expect(liveRunTitle(run({ nextAction: "Drafting reply" }))).toBe("Running — Drafting reply");
    expect(liveRunTitle(run({}))).toBe("Running now");
  });

  it("describes a stalled run with the upstream label + silence age", () => {
    const critical = liveRunTitle(
      run({ outputSilence: { level: "critical", silenceAgeMs: 5 * 60 * 60_000 } }),
    );
    expect(critical).toContain("Stale run");
    expect(critical).toContain("5 hours");

    const suspicious = liveRunTitle(
      run({ outputSilence: { level: "suspicious", silenceAgeMs: 90 * 60_000 } }),
    );
    expect(suspicious).toContain("Silence watch");
    expect(suspicious).toContain("1h 30m");
  });
});

describe("formatSilenceAgeMs", () => {
  it("formats minutes and hours, rejecting non-positive input", () => {
    expect(formatSilenceAgeMs(0)).toBeNull();
    expect(formatSilenceAgeMs(null)).toBeNull();
    expect(formatSilenceAgeMs(undefined)).toBeNull();
    expect(formatSilenceAgeMs(30_000)).toBe("under a minute");
    expect(formatSilenceAgeMs(60_000)).toBe("1 minute");
    expect(formatSilenceAgeMs(3 * 60_000)).toBe("3 minutes");
    expect(formatSilenceAgeMs(60 * 60_000)).toBe("1 hour");
    expect(formatSilenceAgeMs(90 * 60_000)).toBe("1h 30m");
  });
});
