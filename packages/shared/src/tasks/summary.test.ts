import { describe, expect, it } from "vitest";

import schema from "../../fixtures/super-agent/tasks-schema.json";
import { FIXTURES, PRIYA, SAM, fixtureTask, viewAs } from "./fixtures";
import { OUTPUT_KINDS, type TaskSummary } from "./types";
import { answerVerb, askLine, doneByLine, effectLine, formatSchedule, overviewGroups, previewRows, primaryOptionId, specialistLabel, summaryView, taskSummaryLine } from "./view";

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

describe("summaryView: one of four words per status, group from the server", () => {
  const t = (over: Partial<TaskSummary>) => fixtureTask({ taskId: "t", title: "x", ...over });
  it.each([
    [{ status: "proposed", shownGroup: null }, "Needs you", "Wants to start", null, "start", "waiting"],
    [{ status: "queued" }, "Working", "Starting", "working", null, "working"],
    [{ status: "working" }, "Working", null, "working", null, "working"],
    [{ status: "waiting" }, "Working", "Waiting on a reply", "working", null, "working"],
    [{ status: "idle", shownGroup: "idle" }, "Done", null, "idle", null, "neutral"],
    [{ status: "idle", shownGroup: "idle", watching: true, watchUntil: "2026-11-10" }, "Working", "Checking back later", "idle", null, "working"],
    [{ status: "resolved", shownGroup: "resolved" }, "Closed", null, "resolved", "reopen", "neutral"],
    [{ status: "resolved", shownGroup: "resolved", statusReason: "stopped" }, "Closed", "Stopped", "resolved", "reopen", "neutral"],
    [{ status: "working", shownGroup: "landing", landing: "e1" }, "Working", "Applying", "landing", null, "landing"],
    [{ status: "failed", shownGroup: "waiting_on_you" }, "Needs you", "Failed", "waiting_on_you", "retry", "waiting"],
  ] as const)("%j → %s (%s)", (over, word, reason, group, action, tone) => {
    expect(summaryView(t(over as Partial<TaskSummary>), SAM)).toEqual({ word, reason, group, action, tone });
  });

  it("waiting with my open approval is Needs you; a question is Answer", () => {
    const s5 = byId(FIXTURES.step05.tasks, "t-signup");
    expect(summaryView(s5, SAM)).toMatchObject({ word: "Needs you", reason: "Needs approval", group: "waiting_on_you", action: "approve" });
    const q = { ...s5, openQuestion: { ...s5.openQuestion!, kind: "question" as const } };
    expect(summaryView(q, SAM).action).toBe("answer");
  });

  it("waiting for someone else is Working, waiting on them", () => {
    const priya = viewAs(byId(FIXTURES.step05.tasks, "t-signup"), PRIYA);
    expect(summaryView(priya, PRIYA)).toEqual({ word: "Working", reason: "Waiting on Sam", group: "working", action: null, tone: "working" });
  });

  it("review question is Ready for review with the output's next step", () => {
    expect(summaryView(byId(FIXTURES.step07.tasks, "t-email"), SAM))
      .toEqual({ word: "Needs you", reason: "Ready for review", group: "ready_for_review", action: "send", tone: "review" });
  });

  it("failed is Closed for someone else, with no action", () => {
    const failed = viewAs(fixtureTask({ taskId: "t", title: "x", status: "failed", shownGroup: "waiting_on_you" }), PRIYA);
    expect(summaryView(failed, PRIYA)).toMatchObject({ word: "Closed", reason: "Failed", group: "idle", action: null });
  });
});

describe("overviewGroups", () => {
  it("always includes Needs you and Closed (collapsed)", () => {
    const groups = overviewGroups([]);
    expect(groups.map((g) => g.label)).toEqual(["Needs you", "Closed"]);
    expect(groups[1]).toMatchObject({ collapsed: true, alwaysShown: true });
  });
  it("four headings in order; proposed tasks are not in it", () => {
    expect(overviewGroups(FIXTURES.step02.tasks).map((g) => [g.id, g.tasks.length]))
      .toEqual([["needs_you", 0], ["working", 2], ["closed", 0]]);
    expect(overviewGroups(FIXTURES.step07.tasks).map((g) => g.id))
      .toEqual(["needs_you", "working", "done", "closed"]);
  });
});

describe("summary lines and labels", () => {
  it("lines", () => {
    expect(taskSummaryLine(byId(FIXTURES.step05.tasks, "t-signup"), SAM)).toBe("Needs approval: Provision database and Stripe");
    expect(taskSummaryLine(byId(FIXTURES.step09.tasks, "t-email"), SAM)).toBe("Applying: Draft: \"Join us Nov 20\" (240 words)");
    expect(taskSummaryLine(byId(FIXTURES.step02.tasks, "t-signup"), SAM)).toBe("Working");
    expect(taskSummaryLine(byId(FIXTURES.step02.tasks, "t-fees"), SAM)).toBe("would use a paid tool");
  });
  it("actor line for the request pane", () => {
    expect(doneByLine("finance", { word: "Working", reason: null })).toBe("Finance is working on this");
    expect(doneByLine("finance", { word: "Needs you", reason: "Ready for review" })).toBe("Finance is working on this");
    expect(doneByLine("finance", { word: "Done", reason: null })).toBe("Done by Finance");
    expect(doneByLine(null, { word: "Closed", reason: null })).toBe("Done by Lightbulb");
  });
  it("no actor line for what never ran or ended without a result", () => {
    expect(doneByLine("finance", { word: "Needs you", reason: "Wants to start" })).toBeNull();
    for (const reason of ["Stopped", "Dismissed", "Failed"]) expect(doneByLine("finance", { word: "Closed", reason })).toBeNull();
  });
  it("ids that are not names never show", () => {
    for (const id of ["agent_7f3a", "a1b2c3", "5b36216c-196c-48af-ad2a-6817c42a1de3"]) expect(specialistLabel(id)).toBe("Lightbulb");
    expect(specialistLabel("customer success")).toBe("Customer success");
  });
  it("specialist label", () => {
    expect(specialistLabel("finance")).toBe("Finance");
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
  it("shifts the day when the viewer's zone crosses midnight; odd entries never show raw cron", () => {
    expect(formatSchedule(["0 22 * * 1"], "America/Los_Angeles", "Europe/Berlin", NOV)).toBe("Tuesdays 7:00");
    expect(formatSchedule(["0 18 * * 1-5"], "America/Los_Angeles", "Europe/Berlin", NOV)).toBe("Tuesdays to Saturdays 3:00");
    expect(formatSchedule(["0 1 * * 6,0"], "Europe/Berlin", "America/Los_Angeles", NOV)).toBe("Fridays and Saturdays 16:00");
    expect(formatSchedule(["0 9 1 * *"], "UTC", "UTC", NOV)).toBe("Custom schedule");
    expect(formatSchedule(["*/15 * * * *"], "UTC", "UTC", NOV)).toBe("Custom schedule");
    // One odd entry among plain ones: the whole schedule is custom, never "Mondays 9:00 and 0 9 1 * *".
    expect(formatSchedule(["0 9 * * 1", "0 9 1 * *"], "UTC", "UTC", NOV)).toBe("Custom schedule");
    expect(formatSchedule(["27 10 5 11 *"], "America/New_York", "Europe/London", NOV)).toBe("Once, 5 Nov, 15:27");
    expect(formatSchedule(["0 9 * * 1", "0 9 * * 4"], "UTC", "UTC", NOV)).toBe("Mondays 9:00 and Thursdays 9:00");
  });
});

describe("specialistLabel with the agents list", () => {
  it("an agent id reads by its name; an unknown id never shows; a handle reads as itself", () => {
    const id = "5b36216c-196c-48af-ad2a-6817c42a1de3";
    expect(specialistLabel(id, new Map([[id, "Finance"]]))).toBe("Finance");
    expect(specialistLabel(id)).toBe("Lightbulb");
    expect(specialistLabel("finance")).toBe("Finance");
  });
});

describe("questions: the server's option ids and copy", () => {
  it("the one-press option is the question's primary, never a literal", () => {
    // questions.py kind_and_options: approval [allow_once, deny]; review [send|publish|merge, request_changes].
    expect(primaryOptionId([{ id: "allow_once" }, { id: "deny" }])).toBe("allow_once");
    expect(primaryOptionId([{ id: "send" }, { id: "request_changes" }])).toBe("send");
    expect(primaryOptionId([{ id: "deny", style: "danger" }, { id: "allow_once", style: "primary" }])).toBe("allow_once");
    expect(primaryOptionId([])).toBeNull();
  });
  it("an approval names who and the exact target; a decision reads Approved", () => {
    expect(askLine({ kind: "approval", title: "send 'Q3' to a@b.c", preview: { kind: "mail.send" } }, "finance")).toBe("Allow Finance to send 'Q3' to a@b.c");
    // A worker's own approval ask (no preview) keeps its words.
    expect(askLine({ kind: "approval", title: "Can I spend $40 on ads?" }, "ads")).toBe("Can I spend $40 on ads?");
    expect(askLine({ kind: "question", title: "Which date?", text: "" }, "finance")).toBe("Which date?");
    expect(answerVerb("allow_once", "Allow once")).toBe("Approved");
    expect(answerVerb("deny")).toBe("Denied");
    expect(answerVerb("pick_b", "Option B")).toBe("Option B");
  });
  it("the preview shows the effect's operands, not its ids", () => {
    expect(previewRows({ kind: "mail.send", to: ["a@b.c", "d@e.f"], subject: "Q3", idemKey: "x", nested: { a: 1 } }))
      .toEqual([{ label: "To", value: "a@b.c, d@e.f" }, { label: "Subject", value: "Q3" }]);
    expect(previewRows({ kind: "pr.merge", repo: "acme/web", number: 12 })).toEqual([{ label: "Repository", value: "acme/web" }, { label: "PR", value: "#12" }]);
    expect(previewRows(null)).toEqual([]);
  });
  it("one-press Approve only for an effect approval (allow_once); a worker's own options open the thread", () => {
    const t = byId(FIXTURES.step05.tasks, "t-signup");
    expect(summaryView(t, SAM).action).toBe("approve");
    const custom = { ...t, openQuestion: { ...t.openQuestion!, options: [{ id: "cancel_order", label: "Cancel" }, { id: "go_ahead", label: "Go ahead" }] } };
    expect(summaryView(custom, SAM).action).toBe("answer");
    expect(summaryView({ ...t, openQuestion: { ...t.openQuestion!, options: [] } }, SAM).action).toBe("answer");
  });
  it("purchase approvals name the merchant and why", () => {
    expect(previewRows({ kind: "purchase", amount: "$12", merchant: "Namecheap", why: "launch domain", url: "https://x" }))
      .toEqual([{ label: "Amount", value: "$12" }, { label: "Merchant", value: "Namecheap" }, { label: "Why", value: "launch domain" }, { label: "Link", value: "https://x" }]);
  });
  it("a dismissed suggestion reads Dismissed, with no Reopen", () => {
    const t = { ...byId(FIXTURES.step02.tasks, "t-fees"), status: "resolved" as const, statusReason: "dismissed", shownGroup: "resolved" as const };
    expect(summaryView(t, SAM)).toMatchObject({ word: "Closed", reason: "Dismissed", action: null });
  });
});

describe("overviewGroups splits the server's idle group so each heading matches its rows", () => {
  it("watched is Working, someone else's failed is Closed, the rest Done", () => {
    const t = (taskId: string, over: Partial<TaskSummary>) => fixtureTask({ taskId, title: taskId, shownGroup: "idle", status: "idle", ...over });
    const groups = overviewGroups([t("a", {}), t("b", { watching: true, watchUntil: "2026-12-01T00:00:00Z" }), t("c", { status: "failed" })]);
    expect(groups.filter((g) => g.tasks.length).map((g) => [g.label, g.tasks.map((x) => x.taskId)]))
      .toEqual([["Working", ["b"]], ["Done", ["a"]], ["Closed", ["c"]]]);
  });
});
