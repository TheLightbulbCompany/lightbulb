import { describe, expect, it } from "vitest";
import {
  activityRowParts,
  approvalNotificationCopy,
  filterActivity,
  groupActivity,
  interactionActivityId,
  isNeedsYou,
} from "./activityView";
import { attentionActivityCount } from "./useActivity";
import type { ActivityItem } from "./types";

function item(over: Partial<ActivityItem> & { id: string }): ActivityItem {
  return {
    kind: "mention",
    subtype: null,
    roomId: "r1",
    messageId: null,
    threadRootMessageId: null,
    actor: { kind: "human", id: "u1", name: "Ann" },
    createdAt: "2026-09-25T10:00:00Z",
    state: "open",
    resolvedBy: null,
    resolvedAt: null,
    read: false,
    summary: {
      title: "Ann mentioned you in #general",
      requester: { kind: "human", id: "u1", name: "Ann" },
      reason: null,
      preview: { text: "can you look?", destination: null, fields: [] },
      links: [{ kind: "room", id: "r1", label: "#general" }],
    },
    actions: [],
    ...over,
  };
}

const growth = { kind: "agent" as const, id: "a1", name: "Growth" };
const approval = item({
  id: "paperclip:approval:ap1",
  kind: "paperclip",
  subtype: "approval",
  actor: growth,
  createdAt: "2026-09-08T10:00:00Z",
  summary: {
    title: "Growth: Pilot X auto-replies",
    requester: growth,
    reason: "Reply to mentions for a week",
    preview: null,
    links: [{ kind: "approval", id: "ap1", label: "Approval" }],
  },
});
const failedRun = item({
  id: "paperclip:failed_run:r1",
  kind: "paperclip",
  subtype: "failed_run",
  actor: growth,
  summary: { title: "Growth's last run failed", requester: growth, reason: "Setup failed", preview: null, links: [] },
});
const issue = item({
  id: "paperclip:assigned_issue:i1",
  kind: "paperclip",
  subtype: "assigned_issue",
  summary: { title: "LB-12 is assigned to you: Fix", requester: growth, reason: "Status: To do", preview: null, links: [{ kind: "issue", id: "i1", label: "LB-12" }] },
});
const ask = item({
  id: "paperclip:interaction:i1:q1",
  kind: "paperclip",
  subtype: "interaction",
  actor: growth,
  summary: {
    title: "Growth needs your confirmation: Launch email",
    requester: growth,
    reason: "Send it to 2,000 people?",
    preview: null,
    links: [{ kind: "issue", id: "i1", label: "LB-12" }],
  },
});
const mention = item({ id: "mention:1", createdAt: "2026-09-25T09:00:00Z" });
const thread = item({ id: "thread_reply:1", kind: "thread_reply", createdAt: "2026-09-20T09:00:00Z" });

describe("activityView", () => {
  it("needs you is open approvals and failed runs only", () => {
    expect([approval, failedRun, issue, mention].map(isNeedsYou)).toEqual([true, true, false, false]);
    expect(isNeedsYou({ ...approval, state: "resolved" })).toBe(false);
    const waiting = { ...approval, kind: "session" as const, subtype: "waiting" as const };
    expect(isNeedsYou(waiting)).toBe(true);
    expect(isNeedsYou({ ...waiting, state: "resolved" })).toBe(false);
    expect(isNeedsYou({ ...waiting, subtype: "join_request" })).toBe(true);
    expect(isNeedsYou({ ...waiting, subtype: "finished" })).toBe(false);
    expect(isNeedsYou({ ...waiting, subtype: "finished" })).toBe(false);
    const loginAsk = { ...approval, kind: "login" as const, subtype: "login" as const };
    expect(isNeedsYou(loginAsk)).toBe(true);
    expect(isNeedsYou({ ...loginAsk, state: "resolved" })).toBe(false);
  });

  it("an agent's pending question is pinned and counted like an approval", () => {
    expect(isNeedsYou(ask)).toBe(true);
    expect(isNeedsYou({ ...ask, state: "resolved" })).toBe(false);
    expect(filterActivity([ask, issue], "agents")).toEqual([issue]);
    expect(activityRowParts(ask)).toMatchObject({
      lead: "Growth",
      rest: "needs your confirmation: Launch email",
      snippet: "Send it to 2,000 people?",
      location: "LB-12",
      locationLink: { kind: "issue", id: "i1" },
    });
    expect(approvalNotificationCopy(ask)).toEqual({
      title: "Growth · LB-12",
      body: "Needs your confirmation: Launch email",
    });
    expect(interactionActivityId("i1", "q1")).toBe(ask.id);
  });

  it("filters per chip", () => {
    const all = [approval, failedRun, issue, mention, thread];
    expect(filterActivity(all, "needs-you").map((i) => i.id)).toEqual([approval.id, failedRun.id]);
    expect(filterActivity(all, "mentions")).toEqual([mention]);
    expect(filterActivity(all, "threads")).toEqual([thread]);
    expect(filterActivity(all, "agents")).toEqual([issue]);
    expect(filterActivity(all, "all")).toHaveLength(5);
  });

  it("pins needs you above Today and Earlier, even when older", () => {
    const sections = groupActivity([mention, approval, thread], new Date("2026-09-25T12:00:00Z"));
    expect(sections.map((s) => [s.key, s.items.map((i) => i.id)])).toEqual([
      ["needs-you", [approval.id]],
      ["today", [mention.id]],
      ["earlier", [thread.id]],
    ]);
  });

  it("splits the server sentence into Slack's row", () => {
    expect(activityRowParts(approval)).toEqual({
      lead: "Growth",
      rest: "needs your approval",
      snippet: "Pilot X auto-replies — Reply to mentions for a week",
      location: null,
      locationLink: null,
    });
    expect(activityRowParts(mention)).toMatchObject({ lead: "Ann", rest: "mentioned you in #general", location: null, snippet: "can you look?" });
    expect(activityRowParts(failedRun)).toMatchObject({ lead: "Growth", rest: "'s last run failed" });
    expect(activityRowParts(issue)).toMatchObject({ lead: null, rest: "LB-12 is assigned to you: Fix", location: null });
  });

  // It read "sent you a direct message · your conversation".
  it("gives a direct message no place: its title says who, and the place is the two of you", () => {
    const dm: ActivityItem = {
      ...mention, id: "dm:m7:r7:7", kind: "dm",
      summary: {
        ...mention.summary, title: "Ann sent you a direct message",
        links: [{ kind: "room", id: "r7", label: "your conversation" }],
      },
    };
    expect(activityRowParts(dm)).toMatchObject({ lead: "Ann", rest: "sent you a direct message", location: null, locationLink: null });
  });

  it("prefers the why over the payload and names a task it happened on", () => {
    const post = item({
      id: "paperclip:approval:p", kind: "paperclip", subtype: "approval", actor: growth,
      summary: { title: "Growth wants to publish a post", requester: growth, reason: "Launch plan",
        preview: { text: "We shipped", destination: null, fields: [] }, links: [{ kind: "issue", id: "i9", label: "LB-9" }] },
    });
    expect(activityRowParts(post)).toMatchObject({ snippet: "Launch plan", location: "LB-9", locationLink: { kind: "issue", id: "i9" } });
  });

  it("names the agent and the ask in a notification", () => {
    expect(approvalNotificationCopy(approval)).toEqual({
      title: "Growth · Approval",
      body: "Needs your approval: Pilot X auto-replies",
    });
  });

  it("the badge keeps every needs-you item after seen", () => {
    expect(attentionActivityCount([approval, failedRun, mention], "2026-09-26T00:00:00Z")).toBe(2);
    expect(attentionActivityCount([approval, mention], "2026-09-01T00:00:00Z")).toBe(2);
  });

  it("the badge counts needs you, mentions and DMs; thread replies, reactions and finished sessions never", () => {
    const unseen = "2026-09-01T00:00:00Z";
    const dm = item({ id: "dm:1", kind: "dm" });
    const thread = item({ id: "thread_reply:1", kind: "thread_reply" });
    const reaction = item({ id: "reaction:1", kind: "reaction" });
    const finished = item({ id: "session:f1", kind: "session", subtype: "finished" });
    expect(attentionActivityCount([approval, mention, dm, thread, reaction, finished], unseen)).toBe(3);
  });
});

describe("isNeedsYou: super-agent questions", () => {
  it("an open task question needs you; an answered one does not", async () => {
    const { isNeedsYou } = await import("./activityView");
    const base = { id: "taskq:t1:q1", kind: "task_question", subtype: "approval", roomId: null, messageId: null, threadRootMessageId: null,
      actor: { kind: "agent", id: "", name: "Lightbulb" }, createdAt: "2026-11-04T14:00:00Z", resolvedBy: null, resolvedAt: null, read: false,
      summary: { title: "Allow?", requester: null, reason: null, preview: null, links: [{ kind: "task", id: "t1", label: "Signup app" }] }, actions: [] };
    expect(isNeedsYou({ ...base, state: "open" } as never)).toBe(true);
    expect(isNeedsYou({ ...base, state: "answered" } as never)).toBe(false);
  });
});
