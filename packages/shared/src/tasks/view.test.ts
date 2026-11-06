import { describe, expect, it } from "vitest";

import schema from "../../fixtures/super-agent/tasks-schema.json";
import {
  EFFECT_STATES,
  QUESTION_KINDS,
  QUESTION_STATES,
  SHOWN_GROUPS,
  STATUS_WORDS,
  TASK_STATUSES,
  taskView,
  type EffectRow,
  type QuestionRow,
  type TaskRow,
  type TaskStatus,
  type TaskViewInput,
} from "./view";

const SAM = "user-sam";
const PRIYA = "user-priya";
const names: Record<string, string> = { [SAM]: "Sam", [PRIYA]: "Priya" };
const nameOf = (id: string) => names[id] ?? "someone";

const task = (status: TaskStatus, extra: Partial<TaskRow> = {}): TaskRow => ({
  taskId: "t1", status, ownerUserId: SAM, watchUntil: null, ...extra,
});
const q = (kind: QuestionRow["kind"], answerers: string[], state: QuestionRow["state"] = "open"): QuestionRow =>
  ({ taskId: "t1", qid: `q-${kind}-${answerers.join()}`, kind, answerers, state });
const effect = (state: EffectRow["state"], taskId = "t1"): EffectRow => ({ taskId, idemKey: `e-${state}`, state });
const view = (t: TaskRow, viewerId: string, rest: Partial<TaskViewInput> = {}) =>
  taskView({ task: t, viewerId, questions: [], effects: [], nameOf, ...rest });

describe("contract: types match tasks-schema.json", () => {
  it("enums are the schema's, in order", () => {
    expect(TASK_STATUSES).toEqual(schema.enums.TaskStatus);
    expect(SHOWN_GROUPS).toEqual(schema.enums.ShownGroup);
    expect(QUESTION_KINDS).toEqual(schema.enums.QuestionKind);
    expect(QUESTION_STATES).toEqual(schema.enums.QuestionState);
    expect(EFFECT_STATES).toEqual(schema.enums.EffectState);
  });

  it("every row field read here exists on the schema row", () => {
    const fields: Record<string, string[]> = {
      task: ["taskId", "status", "ownerUserId", "watchUntil"],
      question: ["taskId", "qid", "kind", "answerers", "state"],
      effect: ["taskId", "idemKey", "state"],
    };
    const rows = schema.rows as Record<string, { properties: Record<string, unknown> }>;
    for (const [row, names] of Object.entries(fields)) {
      for (const name of names) expect(Object.keys(rows[row].properties), `${row}.${name}`).toContain(name);
    }
  });
});

describe("taskView: one word, group and action per stored status (contracts-v2 §4)", () => {
  it.each([
    // status, viewer, word, reason, group, action
    ["queued", SAM, "Working", "Starting", "working", null],
    ["working", SAM, "Working", null, "working", null],
    ["working", PRIYA, "Working", null, "working", null],
    ["waiting", SAM, "Working", "Waiting on a reply", "working", null],
    ["idle", SAM, "Done", null, "idle", null],
    ["idle", PRIYA, "Done", null, "idle", null],
    ["resolved", SAM, "Closed", null, "resolved", "reopen"],
    ["resolved", PRIYA, "Closed", null, "resolved", "reopen"],
    ["failed", SAM, "Needs you", "Failed", "waiting_on_you", "retry"],
    ["failed", PRIYA, "Closed", "Failed", "idle", null],
  ] as const)("%s as %s → %s (%s) / %s / %s", (status, viewer, word, reason, group, action) => {
    expect(view(task(status), viewer)).toEqual({ word, reason, group, action });
  });

  it("covers every stored status with one of the four words", () => {
    for (const status of TASK_STATUSES) expect(STATUS_WORDS).toContain(view(task(status), SAM).word);
  });

  it("idle with watchUntil is still Working: it wakes itself", () => {
    expect(view(task("idle", { watchUntil: "2026-11-05T00:00:00Z" }), SAM))
      .toEqual({ word: "Working", reason: "Checking back later", group: "idle", action: null });
  });

  it("failed with no owner is Closed in Idle for everyone", () => {
    expect(view(task("failed", { ownerUserId: null }), SAM)).toEqual({ word: "Closed", reason: "Failed", group: "idle", action: null });
  });
});

describe("taskView: open questions", () => {
  it.each([
    ["approval", "Needs approval", "waiting_on_you", "approve"],
    ["question", "Needs an answer", "waiting_on_you", "answer"],
    ["review", "Ready for review", "ready_for_review", "review"],
  ] as const)("%s for me → Needs you (%s)", (kind, reason, group, action) => {
    expect(view(task("waiting"), SAM, { questions: [q(kind, [SAM])] })).toEqual({ word: "Needs you", reason, group, action });
  });

  it.each(["approval", "question", "review"] as const)(
    "%s for someone else → Waiting on <name>, grouped Working, no action", (kind) => {
      expect(view(task("waiting"), PRIYA, { questions: [q(kind, [SAM])] }))
        .toEqual({ word: "Working", reason: "Waiting on Sam", group: "working", action: null });
    });

  it("one of several answerers is enough to make it mine", () => {
    expect(view(task("waiting"), PRIYA, { questions: [q("approval", [SAM, PRIYA])] }).group).toBe("waiting_on_you");
  });

  it("my approval outranks someone else's and my review", () => {
    const questions = [q("approval", [PRIYA]), q("review", [SAM]), q("approval", [SAM])];
    expect(view(task("waiting"), SAM, { questions })).toEqual({ word: "Needs you", reason: "Needs approval", group: "waiting_on_you", action: "approve" });
  });

  it("my review outranks someone else's approval", () => {
    const questions = [q("approval", [PRIYA]), q("review", [SAM])];
    expect(view(task("waiting"), SAM, { questions }).group).toBe("ready_for_review");
  });

  it.each(["answered", "cancelled"] as const)("%s questions are ignored", (state) => {
    expect(view(task("waiting"), SAM, { questions: [q("approval", [SAM], state)] }))
      .toEqual({ word: "Working", reason: "Waiting on a reply", group: "working", action: null });
  });

  it("questions of another task are ignored", () => {
    expect(view(task("waiting"), SAM, { questions: [{ ...q("approval", [SAM]), taskId: "t2" }] }).group).toBe("working");
  });

  it("unknown answerer name falls back through nameOf", () => {
    expect(view(task("waiting"), PRIYA, { questions: [q("approval", ["user-x"])] }).reason).toBe("Waiting on someone");
  });

  it.each([
    [[], "Waiting on a reply"],
    [[SAM], "Waiting on Sam"],
    [[SAM, "user-alex"], "Waiting on Sam or someone"],
    [[SAM, "user-alex", "user-kim"], "Waiting on Sam or 2 others"],
  ] as const)("someone else's answerers %j → %s", (answerers, reason) => {
    const seen: string[] = [];
    const got = view(task("waiting"), PRIYA, {
      questions: [q("approval", [...answerers])],
      nameOf: (id) => { seen.push(id); return nameOf(id); },
    }).reason;
    expect(got).toBe(reason);
    expect(seen).not.toContain(undefined);
  });

  it("names answerers across all open questions once each", () => {
    const questions = [q("approval", [SAM]), q("question", [SAM, "user-alex"])];
    expect(view(task("waiting"), PRIYA, { questions }).reason).toBe("Waiting on Sam or someone");
  });

  it("questions do not change idle, resolved or failed", () => {
    const questions = [q("approval", [SAM])];
    expect(view(task("resolved"), SAM, { questions }).word).toBe("Closed");
    expect(view(task("failed"), SAM, { questions }).reason).toBe("Failed");
    expect(view(task("idle"), SAM, { questions }).word).toBe("Done");
  });
});

describe("taskView: Landing (apply_effect running)", () => {
  it.each(["approved", "intent"] as const)("an %s effect on a live task → Landing", (state) => {
    for (const status of ["queued", "working", "waiting"] as const) {
      expect(view(task(status), SAM, { effects: [effect(state)] })).toEqual({ word: "Working", reason: "Applying", group: "landing", action: null });
    }
  });

  it.each(["proposed", "denied", "done", "unknown"] as const)("a %s effect is not Landing", (state) => {
    expect(view(task("working"), SAM, { effects: [effect(state)] }).group).toBe("working");
  });

  it("another task's effect is not Landing", () => {
    expect(view(task("working"), SAM, { effects: [effect("intent", "t2")] }).group).toBe("working");
  });

  it("a question for me outranks Landing", () => {
    expect(view(task("waiting"), SAM, { effects: [effect("intent")], questions: [q("approval", [SAM])] }).group)
      .toBe("waiting_on_you");
  });

  it("Landing outranks waiting on someone else", () => {
    expect(view(task("waiting"), PRIYA, { effects: [effect("intent")], questions: [q("approval", [SAM])] }).reason)
      .toBe("Applying");
  });

  it("does not change idle, resolved or failed", () => {
    const effects = [effect("intent")];
    expect(view(task("idle"), SAM, { effects }).group).toBe("idle");
    expect(view(task("resolved"), SAM, { effects }).group).toBe("resolved");
    expect(view(task("failed"), SAM, { effects }).group).toBe("waiting_on_you");
  });
});

describe("money in approvals (dev walk W7-9)", () => {
  it("an amount reads with its currency, USD by default, everywhere the purchase is shown", async () => {
    const { askLine, moneyText, previewRows, taskSummaryLine } = await import("./view");
    expect(moneyText("11.81")).toBe("$11.81");
    expect(moneyText(11.81, "eur")).toBe("€11.81");
    expect(moneyText("about 12")).toBe("about 12");
    const q = { kind: "approval", title: "buy austinworkshop2027.com for 11.81", preview: { kind: "purchase", item: "austinworkshop2027.com", amount: "11.81" } };
    expect(askLine(q, "Engineering")).toBe("Allow Engineering to buy austinworkshop2027.com for $11.81");
    expect(previewRows(q.preview)).toContainEqual({ label: "Amount", value: "$11.81" });
    const task = { status: "waiting", shownGroup: "waiting_on_you", openQuestion: { qid: "q", kind: "approval", subkind: "purchase", title: q.title, answerers: ["u"], answererNames: ["U"], canAnswer: true } };
    expect(taskSummaryLine(task as never, "u")).toBe("Needs approval: buy austinworkshop2027.com for $11.81");
  });
});
