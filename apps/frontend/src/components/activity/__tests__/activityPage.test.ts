import { describe, expect, it } from "vitest";
import { FOR_YOU_FILTERS, dayLabel, feedTimeline, filterForYou, forYouEntries, homeExtras, isForYouFilter, splitPost } from "../activityPage";
import { approval, ask, assigned, attention, failedRun, loginAsk, maya, message, post, runGroup } from "./fixtures";

describe("For you", () => {
  it("lists every kind once, newest first, and leaves out what is not for you", () => {
    const entries = forYouEntries(
      [approval, ask, message("m1"), message("m2", maya, true), message("d1", maya, false, "dm"), failedRun, assigned, loginAsk,
        { ...approval, id: "paperclip:approval:done", state: "resolved" }, { ...message("m3"), state: "done" }],
      [attention("x1", "decision"), attention("x2", "blocker_attention")],
      [runGroup("g1", "friday", ["r1", "r2"])],
      (id) => (id === "friday" ? "Friday" : "An agent"),
    );
    // Messages at 11:00, Paperclip at 10:00, attention at 09:00, the failed run at 08:00.
    expect(entries.map((entry) => [entry.id, entry.verb])).toEqual([
      ["mention:m1", "reply"],
      // A read message stays (the Unread toggle hides it); one marked done does not.
      ["mention:m2", "reply"],
      ["dm:d1", "reply"],
      ["paperclip:approval:a1", "decide"],
      ["paperclip:interaction:iss-4:int-1", "decide"],
      ["paperclip:assigned_issue:i9", "do"],
      ["login:abc", "do"],
      ["attention:x1", "decide"],
      ["attention:x2", "do"],
      // Failed runs come from the Inbox's own list (every failure, grouped), not Activity's latest-per-agent.
      ["failed_run:g1", "do"],
    ]);
    expect(entries.at(-1)).toMatchObject({ sourceName: "Friday", text: "Timed out ×2", where: "in Work" });
    // The dot is "not opened" and only messages carry it.
    expect(entries.map((entry) => entry.unread)).toEqual([true, false, true, false, false, false, false, false, false, false]);
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

  describe("chips", () => {
    const entries = forYouEntries(
      [approval, message("m1"), message("m2", maya, true), message("t1", maya, false, "thread_reply"), message("d1", maya, false, "dm"), assigned, loginAsk],
      [attention("x2", "blocker_attention")], [runGroup("g1")]);
    const ids = (filter: Parameters<typeof filterForYou>[1], unread = false) => filterForYou(entries, filter, unread).map((entry) => entry.id);

    it("All is every row", () => {
      expect(ids("all")).toHaveLength(entries.length);
    });
    it("Needs you keeps what the attention desk does not carry: tasks, asks, failed runs; no messages", () => {
      // Approvals, questions and attention items are on the desk rendered beside these rows.
      expect(ids("needs-you")).toEqual(["paperclip:assigned_issue:i9", "login:abc", "failed_run:g1"]);
    });
    it("Mentions, Threads and DMs are one kind of message each, read or not", () => {
      expect(ids("mentions")).toEqual(["mention:m1", "mention:m2"]);
      expect(ids("threads")).toEqual(["thread_reply:t1"]);
      expect(ids("dms")).toEqual(["dm:d1"]);
    });
    it("Unread hides read messages and keeps every open ask", () => {
      expect(ids("mentions", true)).toEqual(["mention:m1"]);
      expect(ids("all", true)).not.toContain("mention:m2");
      expect(ids("needs-you", true)).toEqual(ids("needs-you"));
    });
    it("the URL's filter values are the chips", () => {
      expect(FOR_YOU_FILTERS.map((filter) => filter.label)).toEqual(["All", "Needs you", "Mentions", "Threads", "DMs"]);
      expect(FOR_YOU_FILTERS.every((filter) => isForYouFilter(filter.id))).toBe(true);
      expect(isForYouFilter("reactions")).toBe(false);
    });
  });

  it("reads like Slack: who, where, then the message or the ask", () => {
    const inDm = (item: ReturnType<typeof message>, roomKind: "dm" | "group-dm", label: string) =>
      ({ ...item, roomKind, summary: { ...item.summary, links: [{ kind: "room" as const, id: "room-1", label }] } });
    const [mention, direct, dmMention, group, issue, decide, login] = forYouEntries([message("m1"), inDm(message("d1", maya, false, "dm"), "dm", "your conversation"),
      inDm(message("m9"), "dm", "your conversation"), inDm(message("g1", maya, false, "dm"), "group-dm", "Launch crew"),
      { ...assigned, createdAt: "2026-09-08T10:30:00Z" }, approval, loginAsk], []);
    expect([mention.sourceName, mention.where, mention.text]).toEqual(["Maya", "in #launch", "message m1"]);
    expect([direct.where, direct.text]).toEqual(["in a DM", "message d1"]);
    // A mention inside a 1:1 DM is "in a DM" too, never the room's label.
    expect(dmMention.where).toBe("in a DM");
    expect(group.where).toBe("in Launch crew");
    expect(login.where).toBeNull();
    expect([issue.where, issue.text]).toEqual(["in Work", "LB-9 is assigned to you: Send invoices"]);
    expect([decide.sourceName, decide.text]).toEqual(["Growth", "needs your approval · Pilot X auto-replies"]);
    expect([login.sourceName, login.text]).toEqual(["Friday", "needs the login for supademo.com"]);
  });

  it("is empty when nothing is for you", () => {
    expect(forYouEntries([{ ...message("m1"), state: "done" }, { ...approval, state: "resolved" }], [])).toEqual([]);
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

describe("Home (super-agent): what For you carried that Home shows nowhere else", () => {
  it("unread mentions and replies as one line; open non-request asks as rows; DMs, requests and Paperclip left out", () => {
    const read = message("m-read", maya, true);
    const dm = message("d1", maya, false, "dm");
    const taskQ = { ...loginAsk, id: "task_question:q1", kind: "task_question" as const, subtype: null };
    const { mentions, asks } = homeExtras([message("m1"), read, dm, message("r1", maya, false, "thread_reply"), loginAsk, approval, ask, taskQ]);
    expect(mentions.map((i) => i.id)).toEqual(["mention:m1", "thread_reply:r1"]);
    expect(asks.map((i) => i.id)).toEqual(["login:abc"]);
  });
});
