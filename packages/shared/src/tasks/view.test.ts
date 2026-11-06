import { describe, expect, it } from "vitest";

import schema from "../../fixtures/super-agent/tasks-schema.json";
import type { TaskSummary } from "./types";
import {
  EFFECT_STATES,
  QUESTION_KINDS,
  QUESTION_STATES,
  SHOWN_GROUPS,
  STATUS_WORDS,
  TASK_STATUSES,
  stepLines,
  stepNow,
  taskDoing,
  chipState,
  clockTime,
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
  it("an amount reads with its currency when known, and never a guessed symbol", async () => {
    const { askLine, moneyText, previewRows, taskSummaryLine } = await import("./view");
    expect(moneyText("11.81", "USD")).toBe("$11.81");
    expect(moneyText(11.81, "eur")).toBe("€11.81");
    expect(moneyText("11.81")).toBe("11.81");
    expect(moneyText("about 12", "USD")).toBe("about 12");
    const q = { kind: "approval", title: "buy austinworkshop2027.com for 11.81", preview: { kind: "purchase", item: "austinworkshop2027.com", amount: "11.81", currency: "USD" } };
    expect(askLine(q, "Engineering")).toBe("Allow Engineering to buy austinworkshop2027.com for $11.81");
    expect(previewRows(q.preview)).toContainEqual({ label: "Amount", value: "$11.81" });
    // An older preview without its currency: the amount as written.
    const bare = { ...q, preview: { ...q.preview, currency: undefined } };
    expect(askLine(bare, "Engineering")).toBe("Allow Engineering to buy austinworkshop2027.com for 11.81");
    expect(previewRows(bare.preview)).toContainEqual({ label: "Amount", value: "11.81" });
    // #4885's title already says "$11.81": left alone.
    expect(askLine({ ...q, title: "buy austinworkshop2027.com for $11.81" }, "Engineering")).toBe("Allow Engineering to buy austinworkshop2027.com for $11.81");
    const open = { qid: "q", kind: "approval", subkind: "purchase", title: q.title, answerers: ["u"], answererNames: ["U"], canAnswer: true };
    const task = (openQuestion: object) => ({ status: "waiting", shownGroup: "waiting_on_you", openQuestion }) as never;
    expect(taskSummaryLine(task(open), "u")).toBe("Needs approval: buy austinworkshop2027.com for 11.81");
    expect(taskSummaryLine(task({ ...open, currency: "USD" }), "u")).toBe("Needs approval: buy austinworkshop2027.com for $11.81");
  });
  it("only a purchase or spend preview formats its amount as money", async () => {
    const { previewRows } = await import("./view");
    expect(previewRows({ kind: "spend", amount: 5, currency: "USD" })).toContainEqual({ label: "Amount", value: "$5.00" });
    expect(previewRows({ kind: "provision", amount: 3, currency: "USD" })).toContainEqual({ label: "Amount", value: "3" });
  });
});

describe("the one decision card", () => {
  it("asks the title, never the text, for a worker's own question", async () => {
    const { askLine } = await import("./view");
    const q = { kind: "question", title: "What is the workshop's exact new date?", text: "I'll use it in the test email." };
    expect(askLine(q, "Growth")).toBe("What is the workshop's exact new date?");
    expect(askLine({ ...q, title: "" }, "Growth")).toBe("I'll use it in the test email.");
  });
  it("puts Deny first and shows Always allow disabled when it can't be remembered", async () => {
    const { decisionButtons, ALWAYS_ALLOW_UNAVAILABLE } = await import("./view");
    const opts = [{ id: "allow_once", label: "Allow once", style: "primary" }, { id: "deny", label: "Deny", style: "danger" }];
    // An outside action that names no fixed target: Always allow shows, disabled with the reason.
    expect(decisionButtons(opts, "a2a", { kind: "a2a.send" })).toEqual([
      { id: "deny", label: "Deny", style: "danger" },
      { id: "allow_once", label: "Allow once", style: "primary" },
      { id: "allow_always", label: "Always allow", style: "secondary", disabledReason: ALWAYS_ALLOW_UNAVAILABLE },
    ]);
    const bindable = [opts[0], { id: "allow_always", label: "Always allow", style: "secondary" }, opts[1]];
    expect(decisionButtons(bindable).map((b) => [b.id, b.disabledReason])).toEqual([["deny", undefined], ["allow_once", undefined], ["allow_always", undefined]]);
    // A review has no Allow once: nothing is added.
    expect(decisionButtons([{ id: "send", label: "Send" }, { id: "request_changes", label: "Request changes" }]).map((b) => b.id)).toEqual(["request_changes", "send"]);
    // Money and provider consent are never remembered: no Always allow at all, even one the server sent.
    expect(decisionButtons(opts, "purchase").map((b) => b.id)).toEqual(["deny", "allow_once"]);
    expect(decisionButtons(bindable, "provider_consent").map((b) => b.id)).toEqual(["deny", "allow_once"]);
    // A Worker's own ask (dev, W12-9): `preview: null`, no rule can cover it, so no Always allow and no false reason.
    expect(decisionButtons(opts, "send", null).map((b) => b.id)).toEqual(["deny", "allow_once"]);
    expect(decisionButtons(opts).map((b) => b.id)).toEqual(["deny", "allow_once"]);
    // A card made before Always allow existed (W11-3): one fixed recipient, no allow_always option, so no wrong reason.
    expect(decisionButtons(opts, "send", { kind: "mail.send", to: "ann@example.com" }).map((b) => b.id)).toEqual(["deny", "allow_once"]);
    expect(decisionButtons(opts, "send", { kind: "mail.send", to: "" }).at(-1)?.disabledReason).toBe(ALWAYS_ALLOW_UNAVAILABLE);
  });
  it("names who asks and how long ago", async () => {
    const { decisionMeta } = await import("./view");
    expect(decisionMeta({ createdAt: new Date(0).toISOString() }, "Growth", undefined, 7 * 3600 * 1000)).toBe("Growth · 7h ago");
    expect(decisionMeta({ createdAt: new Date(0).toISOString() }, "Growth", undefined, 7 * 3600 * 1000, "Spring sale")).toBe("Growth · Spring sale · 7h ago");
  });
  it("splits a Worker's labelled email ask into To, Subject and the body (W13-4)", async () => {
    const { askFields, previewRows } = await import("./view");
    const walk = "Recipient: sa-test-2+clerk_test@thelightbulb.company Subject: New Austin workshop date: March 1, 2027 Body: Hi, The Austin workshop has a new date. To: everyone\nBest, The team";
    const parsed = askFields(walk)!;
    expect(parsed.intro).toBe("");
    expect(previewRows(parsed.preview, Infinity)).toEqual([
      { label: "To", value: "sa-test-2+clerk_test@thelightbulb.company" },
      { label: "Subject", value: "New Austin workshop date: March 1, 2027" },
      // The body runs to the end: a "To:" inside it stays in it.
      { label: "Message", value: "Hi, The Austin workshop has a new date. To: everyone\nBest, The team" },
    ]);
    expect(askFields("Please check.\nTo: ann@example.com\nSubject: Hi")).toEqual({ intro: "Please check.", preview: { to: "ann@example.com", subject: "Hi" } });
    // The prose shape (W14): "Send “…” only to <email>. <the body>".
    expect(askFields("Send \u201c[TEST] Austin\u201d only to sa-test-2+clerk_test@x.company. The body labels it TEST ONLY.")).toEqual({
      intro: "", preview: { to: "sa-test-2+clerk_test@x.company", subject: "[TEST] Austin", body: "The body labels it TEST ONLY." } });
    expect(askFields("Send \u201cHi\u201d to ann@example.com")?.preview).toEqual({ to: "ann@example.com", subject: "Hi" });
    // Prose without that shape, or a recipient without a subject, is left as written.
    expect(askFields("Send it to ann@example.com when ready.")).toBeNull();
    expect(askFields("Reply To: ann@example.com when done")).toBeNull();
    expect(askFields("To: x Subject: <img src=x>")?.preview.subject).toBe("<img src=x>");
  });
});

describe("stepLines (dev walk 12, W12-7)", () => {
  it("hides skill reads and tool lookups behind real work and merges the runs between them", () => {
    const run = ["skill_view", "skill_view", "skill_view", "tool_describe", "skill_view", "skill_view",
      "terminal", "terminal", "terminal", "terminal", "write_file"].map((name) => ({ name, ok: true }));
    expect(stepLines(run)).toEqual(["Wrote a file"]);
  });
  it("names overhead plainly when it is all there is: its latest line, so real work never lowers the count (W15-11)", () => {
    expect(stepLines(["skill_view", "skills_list", "tool_describe"])).toEqual(["Prepared tools"]);
    expect(stepLines(["skill_view", "terminal"])).toEqual(["Worked on it"]);
    expect(stepLines(["skill_view", "terminal", "web_search"])).toEqual(["Searched the web"]);
  });
});

describe("chipState (dev walk 12, W12-1)", () => {
  const at = (s: number) => new Date(Date.UTC(2026, 10, 6, 5, 9, s)).toISOString();
  const now = Date.parse(at(38));
  const summary = (extra: Partial<TaskSummary>): TaskSummary => ({
    taskId: "t1", identifier: "T-1", title: "Taglines", status: "working", shownGroup: "working",
    audience: { kind: "room", roomId: "r1" }, audienceKind: "room", taskRoomId: "tr", watching: false,
    updatedAt: at(0), version: 1, ...extra,
  });
  it("ticks while working and keeps the run's length once done", () => {
    const run = { n: 1, status: "running" as const, startedAt: at(0) };
    expect(chipState(summary({ activity: run }), SAM, now)).toEqual({ mark: "working", word: "Working", time: "0:38" });
    expect(chipState(summary({ status: "idle", shownGroup: "idle", activity: { ...run, status: "ended", endedAt: at(62) } }), SAM, now))
      .toEqual({ mark: "done", word: "Done", time: "1:02" });
  });
  it("says Didn't finish for a failed task, and no time before a run starts", () => {
    expect(chipState(summary({ status: "failed", ownerUserId: SAM }), SAM, now).word).toBe("Didn't finish");
    expect(chipState(summary({ status: "queued" }), SAM, now)).toEqual({ mark: "working", word: "Working", time: null });
  });
  it("W17: ticks from the ask before the first run starts (never a bare Working), not for a retry's wait", () => {
    expect(chipState(summary({ status: "queued", createdAt: at(38) }), SAM, now)).toEqual({ mark: "working", word: "Working", time: "0:00" });
    expect(chipState(summary({ status: "queued", createdAt: at(36), activity: { n: 1, status: "dispatched" } }), SAM, now).time).toBe("0:02");
    expect(chipState(summary({ status: "queued", createdAt: at(0), activity: { n: 2, status: "dispatched" } }), SAM, now).time).toBeNull();
    // Once it runs, the run's start; once it ends, the stored length, whenever it is read.
    expect(chipState(summary({ createdAt: at(0), activity: { n: 1, status: "running", startedAt: at(2) } }), SAM, now).time).toBe("0:36");
    const done = summary({ status: "idle", shownGroup: "idle", createdAt: at(0), activity: { n: 1, status: "ended", startedAt: at(1), endedAt: at(22.9) } });
    expect(chipState(done, SAM, Date.parse(at(23))).time).toBe("0:21");
    expect(chipState(done, SAM, now + 600_000).time).toBe("0:21");
  });
  it("a closed task says Closed, as its list group and its header do (dev walk 13, W13-3)", () => {
    const ended = { n: 1, status: "ended" as const, startedAt: at(0), endedAt: at(57) };
    expect(chipState(summary({ status: "resolved", shownGroup: "resolved", activity: ended }), SAM, now))
      .toEqual({ mark: "done", word: "Closed", time: "0:57" });
  });
  it("formats hours", () => expect(clockTime(3_725_000)).toBe("1:02:05"));
});

describe("stepLines (dev walk 10, W10-3)", () => {
  it("groups runs of one tool and names them for people", () => {
    const run = [
      { name: "skill_view", ok: true },
      ...Array.from({ length: 9 }, () => ({ name: "web_search", ok: true })),
      { name: "web_extract", ok: true }, { name: "web_extract", ok: true },
      { name: "terminal", ok: true }, { name: "terminal", ok: false },
    ];
    expect(stepLines(run)).toEqual(["Searched the web · 9", "Read 2 pages", "Worked on it · failed"]);
    // Commands alone: one line, never "Ran 5 commands" for writing an email (W14).
    expect(stepLines(["terminal", "terminal", "execute_code"])).toEqual(["Worked on it"]);
    expect(stepLines(["mcp__lightbulb__web_search", {}])).toEqual(["Searched the web", "A step"]);
  });
});

describe("taskDoing (W16-11: 'Worked on it' while still working)", () => {
  it("says the work from the task's own verb, else what it works on", () => {
    expect(taskDoing("Draft parent email for Saturday story-time")).toBe("Drafting parent email for Saturday story-time");
    expect(taskDoing("Write Instagram caption")).toBe("Writing Instagram caption");
    expect(taskDoing("Plan the menu")).toBe("Planning the menu");
    expect(taskDoing("Set up the shop page")).toBe("Setting up the shop page");
    expect(taskDoing("Instagram caption for Saturday")).toBe("Working on Instagram caption for Saturday");
    expect(stepNow([{ name: "terminal" }], "Draft parent email")).toBe("Drafting parent email…");
  });
});

describe("stepNow (dev walk 13, W13-5)", () => {
  it("says the latest step as it happens, in the steps' own words", () => {
    expect(stepNow([])).toBe("Getting started…");
    expect(stepNow([{ name: "terminal" }, { name: "web_search" }])).toBe("Searching the web…");
    expect(stepNow([{ name: "terminal", ok: true }])).toBe("Working on it…");
    expect(stepNow(["write_file"])).toBe("Writing a file…");
    expect(stepNow(["skill_view"])).toBe("Checking how to do it…");
    expect(stepNow(["tool_describe"])).toBe("Preparing tools…");
  });
});
