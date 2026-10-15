import { describe, expect, it } from "vitest";
import { dayLabel, feedTimeline, filterForYou, forYouEntries, groupForYou, rowSummary, splitPost } from "../activityPage";
import { approval, ask, assigned, attention, failedRun, loginAsk, maya, message, post, runGroup } from "./fixtures";

describe("For you", () => {
  it("sorts every kind into Decide, Reply or Do and leaves out what is not for you", () => {
    const entries = forYouEntries(
      [approval, ask, message("m1"), message("m2", maya, true), message("d1", maya, false, "dm"), failedRun, assigned, loginAsk,
        { ...approval, id: "paperclip:approval:done", state: "resolved" }],
      [attention("x1", "decision"), attention("x2", "blocker_attention")],
      [runGroup("g1", "friday", ["r1", "r2"])],
      (id) => (id === "friday" ? "Friday" : "An agent"),
    );
    expect(entries.map((entry) => [entry.id, entry.verb])).toEqual([
      ["paperclip:approval:a1", "decide"],
      ["paperclip:interaction:iss-4:int-1", "decide"],
      ["mention:m1", "reply"],
      ["dm:d1", "reply"],
      ["paperclip:assigned_issue:i9", "do"],
      ["login:abc", "do"],
      ["attention:x1", "decide"],
      ["attention:x2", "do"],
      // Failed runs come from the Inbox's own list (every failure, grouped), not Activity's latest-per-agent.
      ["failed_run:g1", "do"],
    ]);
    expect(entries.at(-1)).toMatchObject({ sourceName: "Friday", text: "Timed out ×2" });
  });

  it("lists an approval or question from the attention feed only when Activity does not (its Paperclip half was down)", () => {
    const sameApproval = { ...attention("x3", "approval"), subject: { ...attention("x3", "approval").subject, id: "a1" } };
    const sameAsk = { ...attention("x4", "issue_thread_interaction"),
      subject: { ...attention("x4", "issue_thread_interaction").subject, id: "int-1" },
      relatedIssue: { ...attention("x4", "issue_thread_interaction").relatedIssue!, id: "iss-4" } };
    // Healthy: Activity lists both, so the attention copies are dropped by id.
    expect(forYouEntries([approval, ask], [sameApproval, sameAsk]).map((entry) => entry.id))
      .toEqual(["paperclip:approval:a1", "paperclip:interaction:iss-4:int-1"]);
    // Paperclip unavailable: Activity has no Paperclip rows, and the attention feed's copies stand in.
    expect(forYouEntries([message("m1")], [sameApproval, sameAsk]).map((entry) => [entry.id, entry.verb])).toEqual([
      ["mention:m1", "reply"], ["attention:x3", "decide"], ["attention:x4", "decide"],
    ]);
  });

  it("filters to the actionable items for needs-you, and to one kind of message for the others", () => {
    const entries = forYouEntries([approval, message("m1"), message("t1", maya, false, "thread_reply"), message("d1", maya, false, "dm"), assigned, loginAsk],
      [attention("x2", "blocker_attention")], [runGroup("g1")]);
    expect(filterForYou(entries, "needs-you").map((entry) => entry.id)).toEqual([
      "paperclip:approval:a1", "paperclip:assigned_issue:i9", "login:abc", "attention:x2", "failed_run:g1",
    ]);
    expect(filterForYou(entries, "mentions").map((entry) => entry.id)).toEqual(["mention:m1"]);
    expect(filterForYou(entries, "threads").map((entry) => entry.id)).toEqual(["thread_reply:t1"]);
    expect(filterForYou(entries, "dms").map((entry) => entry.id)).toEqual(["dm:d1"]);
    expect(filterForYou(entries, "all")).toHaveLength(entries.length);
  });

  it("reads like a one-line row: the source, then what it is", () => {
    const [mention, issue] = forYouEntries([message("m1"), assigned], []);
    expect([mention.sourceName, mention.text]).toEqual(["Maya", "message m1"]);
    expect([issue.sourceName, issue.text]).toEqual(["Maya", "LB-9 is assigned to you: Send invoices"]);
  });

  it("collapses one source's items into one row per verb, in verb order, with counts", () => {
    const groups = groupForYou(forYouEntries(
      [message("m1"), assigned, approval, message("m2"), message("m3"), message("p1", { kind: "human", id: "user_pat", name: "Pat" })],
      [],
    ));
    expect(groups.map((group) => [group.label, group.count])).toEqual([["Decide", 1], ["Reply", 4], ["Do", 1]]);
    const reply = groups[1];
    expect(reply.rows.map((row) => [row.sourceName, row.entries.length])).toEqual([["Maya", 3], ["Pat", 1]]);
    // Same person, different verb: a separate row in its own group.
    expect(groups[2].rows[0]).toMatchObject({ sourceName: "Maya", entries: [expect.objectContaining({ id: assigned.id })] });
  });

  it("names a collapsed row by its kind when every item is one kind, else by the verb", () => {
    const rows = (entries: ReturnType<typeof forYouEntries>) => groupForYou(entries).flatMap((group) => group.rows);
    const approvals = rows(forYouEntries([approval, { ...approval, id: "paperclip:approval:a2" }, { ...approval, id: "paperclip:approval:a3" }], []));
    expect(rowSummary(approvals[0])).toBe("3 approvals");
    const messages = rows(forYouEntries([message("m1"), message("d1", maya, false, "dm")], []));
    expect(rowSummary(messages[0])).toBe("2 messages");
    const runs = rows(forYouEntries([], [], [runGroup("g1"), runGroup("g2", "friday", ["r9"], "Out of credits")]));
    expect(rowSummary(runs[0])).toBe("2 failed runs");
    const mixed = rows(forYouEntries([{ ...assigned, actor: { kind: "agent", id: "friday", name: "Friday" } }, loginAsk], [], [runGroup("g1")]));
    expect(rowSummary(mixed[0])).toBe("3 things to do");
  });

  it("is empty when nothing needs you", () => {
    expect(groupForYou(forYouEntries([message("m1", maya, true)], []))).toEqual([]);
  });
});

describe("feed timeline", () => {
  const now = new Date("2026-10-15T12:00:00");
  it("lists top-level posts newest first, new ones under one divider, the rest under their day", () => {
    const messages = [
      post(1, { createdAt: "2026-10-13T09:00:00" }),
      post(2, { createdAt: "2026-10-14T09:00:00" }),
      post(3, { createdAt: "2026-10-15T08:00:00" }),
      post(4, { createdAt: "2026-10-15T09:00:00", threadRootMessageId: "post-3" }),
      post(5, { createdAt: "2026-10-15T10:00:00" }),
      post(6, { createdAt: "2026-10-15T11:00:00", deletedAt: "2026-10-15T11:30:00" }),
    ];
    const timeline = feedTimeline(messages, 3, now);
    expect(timeline.map((entry) => entry.type === "divider" ? entry.label : entry.post.messageId)).toEqual([
      "New since you looked", "post-5",
      "Today", "post-3",
      "Yesterday", "post-2",
      dayLabel("2026-10-13T09:00:00", now), "post-1",
    ]);
  });

  it("shows no new divider for a reader who never opened the feed", () => {
    const timeline = feedTimeline([post(1, { createdAt: "2026-10-15T08:00:00" })], 0, now);
    expect(timeline.map((entry) => entry.type === "divider" ? entry.label : entry.post.messageId)).toEqual(["Today", "post-1"]);
  });

  it("takes the first line as the title without its emphasis marks", () => {
    expect(splitPost("**Acme paid: $1,200**\nStripe confirmed it.\nFirst revenue.")).toEqual({ title: "Acme paid: $1,200", body: "Stripe confirmed it.\nFirst revenue." });
    expect(splitPost("# Heading only")).toEqual({ title: "Heading only", body: "" });
  });
});
