import { describe, expect, it } from "vitest";

import schema from "../../fixtures/super-agent/tasks-schema.json";
import { FIXTURES, PRIYA, SAM, fixtureTask, viewAs } from "./fixtures";
import { OUTPUT_KINDS, type TaskSummary } from "./types";
import { effectLine, formatSchedule, overviewGroups, specialistLabel, summaryView, taskSummaryLine } from "./view";

const byId = (ts: TaskSummary[], id: string) => ts.find((t) => t.taskId === id)!;

describe("contract: TaskSummary matches views.TaskSummary in tasks-schema.json", () => {
  it("same field names, same required set, output kinds", () => {
    const view = (schema as unknown as { views: { TaskSummary: { properties: object; required: string[] } } }).views.TaskSummary;
    const sample = fixtureTask({ taskId: "t", title: "x" });
    expect(Object.keys(sample).sort()).toEqual(Object.keys(view.properties).sort());
    for (const f of view.required) expect(sample, f).toHaveProperty(f);
    expect(OUTPUT_KINDS).toEqual(schema.enums.OutputKind);
  });
});

describe("summaryView: one word per status, group from the server", () => {
  const t = (over: Partial<TaskSummary>) => fixtureTask({ taskId: "t", title: "x", ...over });
  it.each([
    [{ status: "proposed", shownGroup: null }, "Suggested", null, "start", "neutral"],
    [{ status: "queued" }, "Queued", "working", null, "working"],
    [{ status: "working" }, "Working", "working", null, "working"],
    [{ status: "idle", shownGroup: "idle" }, "Idle", "idle", null, "neutral"],
    [{ status: "idle", shownGroup: "idle", watching: true, watchUntil: "2026-11-10" }, "Watching", "idle", null, "neutral"],
    [{ status: "resolved", shownGroup: "resolved" }, "Resolved", "resolved", "reopen", "neutral"],
    [{ status: "resolved", shownGroup: "resolved", statusReason: "stopped" }, "Stopped", "resolved", "reopen", "neutral"],
    [{ status: "working", shownGroup: "landing", landing: "e1" }, "Applying", "landing", null, "landing"],
    [{ status: "failed", shownGroup: "waiting_on_you" }, "Failed", "waiting_on_you", "retry", "waiting"],
  ] as const)("%j → %s", (over, word, group, action, tone) => {
    expect(summaryView(t(over as Partial<TaskSummary>), SAM)).toEqual({ word, group, action, tone });
  });

  it("waiting with my open approval is Waiting on you; a question is Answer", () => {
    const s5 = byId(FIXTURES.step05.tasks, "t-signup");
    expect(summaryView(s5, SAM)).toMatchObject({ word: "Waiting on you", group: "waiting_on_you", action: "approve" });
    const q = { ...s5, openQuestion: { ...s5.openQuestion!, kind: "question" as const } };
    expect(summaryView(q, SAM).action).toBe("answer");
  });

  it("waiting for someone else is not Waiting on you", () => {
    const priya = viewAs(byId(FIXTURES.step05.tasks, "t-signup"), PRIYA);
    expect(summaryView(priya, PRIYA)).toEqual({ word: "Waiting on Sam", group: "working", action: null, tone: "working" });
  });

  it("review question is Ready for review with the output's next step", () => {
    expect(summaryView(byId(FIXTURES.step07.tasks, "t-email"), SAM))
      .toEqual({ word: "Ready for review", group: "ready_for_review", action: "send", tone: "review" });
  });

  it("failed is someone else's Idle with no action", () => {
    const failed = viewAs(fixtureTask({ taskId: "t", title: "x", status: "failed", shownGroup: "waiting_on_you" }), PRIYA);
    expect(summaryView(failed, PRIYA)).toMatchObject({ group: "idle", action: null });
  });
});

describe("overviewGroups", () => {
  it("always includes Waiting on you and Resolved (collapsed)", () => {
    const groups = overviewGroups([]);
    expect(groups.map((g) => g.id)).toEqual(["waiting_on_you", "resolved"]);
    expect(groups[1]).toMatchObject({ collapsed: true, alwaysShown: true });
  });
  it("contract order; proposed tasks are not in it", () => {
    expect(overviewGroups(FIXTURES.step02.tasks).map((g) => [g.id, g.tasks.length]))
      .toEqual([["waiting_on_you", 0], ["working", 2], ["resolved", 0]]);
    expect(overviewGroups(FIXTURES.step07.tasks).map((g) => g.id))
      .toEqual(["waiting_on_you", "working", "ready_for_review", "idle", "resolved"]);
  });
});

describe("summary lines and labels", () => {
  it("lines", () => {
    expect(taskSummaryLine(byId(FIXTURES.step05.tasks, "t-signup"), SAM)).toBe("Needs approval: Provision database and Stripe");
    expect(taskSummaryLine(byId(FIXTURES.step09.tasks, "t-email"), SAM)).toBe("Applying: Draft: \"Join us Nov 20\" (240 words)");
    expect(taskSummaryLine(byId(FIXTURES.step02.tasks, "t-signup"), SAM)).toBe("Working · Lightbulb · Engineering");
    expect(taskSummaryLine(byId(FIXTURES.step02.tasks, "t-fees"), SAM)).toBe("would use a paid tool");
  });
  it("specialist label", () => {
    expect(specialistLabel("finance")).toBe("Lightbulb · Finance");
    expect(specialistLabel(null)).toBe("Lightbulb");
  });
});

describe("effectLine (lanes/Q.md ruling, 5 Nov)", () => {
  it("a refused done row did not happen; unknown says to check first", () => {
    expect(effectLine({ state: "done", resultRef: "refused:rate_limited" })).toBe("Didn't happen: rate limited");
    expect(effectLine({ state: "done", resultRef: "refused:" })).toBe("Didn't happen: refused");
    expect(effectLine({ state: "done", resultRef: "s3://out" })).toBe("Done");
    expect(effectLine({ state: "unknown", resultRef: null })).toBe("Might have happened — check before retrying");
    expect(effectLine({ state: "intent" })).toBe("Applying");
  });
});

describe("formatSchedule", () => {
  const NOV = Date.UTC(2026, 10, 5, 12); // a Thursday; New York on standard time, London on GMT
  it("reads the row's wall clock in the viewer's zone", () => {
    expect(formatSchedule(["0 9 * * 1"], "America/New_York", "America/New_York", NOV)).toBe("Mondays 9:00");
    expect(formatSchedule(["0 9 * * 1"], "America/New_York", "Europe/London", NOV)).toBe("Mondays 14:00");
    expect(formatSchedule(["30 18 * * 1-5"], "Europe/London", "Europe/London", NOV)).toBe("Weekdays 18:30");
    expect(formatSchedule(["0 7 * * *"], "Asia/Tokyo", "Asia/Tokyo", NOV)).toBe("Every day 7:00");
  });
  it("shifts the day when the viewer's zone crosses midnight; odd entries stay raw", () => {
    expect(formatSchedule(["0 22 * * 1"], "America/Los_Angeles", "Europe/Berlin", NOV)).toBe("Tuesdays 7:00");
    expect(formatSchedule(["0 9 1 * *"], "UTC", "UTC", NOV)).toBe("0 9 1 * *");
    expect(formatSchedule(["0 9 * * 1", "0 9 * * 4"], "UTC", "UTC", NOV)).toBe("Mondays 9:00 and Thursdays 9:00");
  });
});

describe("specialistLabel", () => {
  it("names a handle; an id never shows as a name", () => {
    expect(specialistLabel("finance")).toBe("Lightbulb · Finance");
    expect(specialistLabel("3f2a9c1e-0b4d-4e7a-9a51-2f6c8d0e1b2a")).toBe("Lightbulb");
    expect(specialistLabel("agent_01HXYZABCDEFGH")).toBe("Lightbulb");
  });
});
