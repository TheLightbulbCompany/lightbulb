import { describe, it, expect } from "vitest";
import {
  liveRunActivity,
  liveRunTitle,
  formatSilenceAgeMs,
  buildLiveIssueState,
  type LiveRun,
} from "../liveRuns";

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

  it("never announces a queued run as running", () => {
    expect(liveRunTitle(run({ status: "queued" }))).toBe("Queued — waiting to start");
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
  it("formats minutes and hours, rejecting non-positive input (verbatim upstream copy)", () => {
    expect(formatSilenceAgeMs(0)).toBeNull();
    expect(formatSilenceAgeMs(null)).toBeNull();
    expect(formatSilenceAgeMs(undefined)).toBeNull();
    expect(formatSilenceAgeMs(30_000)).toBe("under 1 minute");
    expect(formatSilenceAgeMs(60_000)).toBe("1 minute");
    expect(formatSilenceAgeMs(3 * 60_000)).toBe("3 minutes");
    expect(formatSilenceAgeMs(60 * 60_000)).toBe("1 hour");
    expect(formatSilenceAgeMs(90 * 60_000)).toBe("1h 30m");
  });
});

describe("buildLiveIssueState", () => {
  it("keys active runs by issue and skips terminal / issueless rows", () => {
    const state = buildLiveIssueState([
      run({ id: "a", issueId: "i1" }),
      run({ id: "b", issueId: "i2", status: "queued" }),
      run({ id: "c", issueId: "i3", status: "succeeded" }),
      run({ id: "d", issueId: null }),
    ]);
    expect(state.get("i1")).toEqual({ activity: "running", title: "Running now" });
    expect(state.get("i2")).toEqual({ activity: "running", title: "Queued — waiting to start" });
    expect(state.has("i3")).toBe(false);
    expect(state.size).toBe(2);
  });

  it("stalled beats healthy for the same issue, in either feed order", () => {
    const healthy = run({ id: "a", issueId: "i1" });
    const stalled = run({
      id: "b",
      issueId: "i1",
      outputSilence: { level: "suspicious", silenceAgeMs: 90 * 60_000 },
    });
    for (const order of [[healthy, stalled], [stalled, healthy]]) {
      const state = buildLiveIssueState(order);
      expect(state.get("i1")?.activity).toBe("stalled");
      expect(state.get("i1")?.title).toContain("Silence watch");
    }
  });

  it("critical beats suspicious for the same issue, in either feed order", () => {
    const suspicious = run({
      id: "a",
      issueId: "i1",
      outputSilence: { level: "suspicious", silenceAgeMs: 20 * 60_000 },
    });
    const critical = run({
      id: "b",
      issueId: "i1",
      outputSilence: { level: "critical", silenceAgeMs: 5 * 60 * 60_000 },
    });
    for (const order of [[suspicious, critical], [critical, suspicious]]) {
      const state = buildLiveIssueState(order);
      expect(state.get("i1")?.title).toContain("Stale run");
      expect(state.get("i1")?.title).toContain("5 hours");
    }
  });
});
