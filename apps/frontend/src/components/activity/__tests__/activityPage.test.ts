import { describe, expect, it } from "vitest";
import { dayLabel, feedTimeline, forYouEntries, homeExtras, splitPost } from "../activityPage";
import { approval, ask, assigned, failedRun, loginAsk, maya, message, post } from "./fixtures";

describe("For you", () => {
  it("lists every kind once, newest first, and leaves out what is not for you", () => {
    const entries = forYouEntries(
      [approval, ask, message("m1"), message("m2", maya, true), message("d1", maya, false, "dm"), failedRun, assigned, loginAsk,
        { ...approval, id: "paperclip:approval:done", state: "resolved" }, { ...message("m3"), state: "done" }],
    );
    // Messages at 11:00, Paperclip at 10:00. A failed run is not an ask.
    expect(entries.map((entry) => [entry.id, entry.verb])).toEqual([
      ["mention:m1", "reply"],
      // A read message stays (the Unread toggle hides it); one marked done does not.
      ["mention:m2", "reply"],
      ["dm:d1", "reply"],
      ["paperclip:approval:a1", "decide"],
      ["paperclip:interaction:iss-4:int-1", "decide"],
      ["paperclip:assigned_issue:i9", "do"],
      ["login:abc", "do"],
    ]);
    // The dot is "not opened" and only messages carry it.
    expect(entries.map((entry) => entry.unread)).toEqual([true, false, true, false, false, false, false]);
  });

  it("reads like Slack: who, where, then the message or the ask", () => {
    const inDm = (item: ReturnType<typeof message>, roomKind: "dm" | "group-dm", label: string) =>
      ({ ...item, roomKind, summary: { ...item.summary, links: [{ kind: "room" as const, id: "room-1", label }] } });
    const [mention, direct, dmMention, group, issue, decide, login] = forYouEntries([message("m1"), inDm(message("d1", maya, false, "dm"), "dm", "your conversation"),
      inDm(message("m9"), "dm", "your conversation"), inDm(message("g1", maya, false, "dm"), "group-dm", "Launch crew"),
      { ...assigned, createdAt: "2026-09-08T10:30:00Z" }, approval, loginAsk]);
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
    expect(forYouEntries([{ ...message("m1"), state: "done" }, { ...approval, state: "resolved" }])).toEqual([]);
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
