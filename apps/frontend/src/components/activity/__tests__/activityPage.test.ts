import { describe, expect, it } from "vitest";
import { forYouEntries, homeExtras } from "../activityPage";
import { approval, ask, assigned, failedRun, loginAsk, maya, message } from "./fixtures";

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

describe("Home (super-agent): what For you carried that Home shows nowhere else", () => {
  it("open non-request asks as rows; mentions, DMs, requests and Paperclip left out", () => {
    const read = message("m-read", maya, true);
    const dm = message("d1", maya, false, "dm");
    const taskQ = { ...loginAsk, id: "task_question:q1", kind: "task_question" as const, subtype: null };
    const { asks } = homeExtras([message("m1"), read, dm, message("r1", maya, false, "thread_reply"), loginAsk, approval, ask, taskQ]);
    expect(asks.map((i) => i.id)).toEqual(["login:abc"]);
  });
});
