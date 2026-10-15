import type { ActivityItem, CanonicalRoomMessage } from "@lightbulb/shared";
import type { AttentionItem } from "@/components/teams/shared/types";

export const growth = { kind: "agent" as const, id: "growth", name: "Growth" };
export const maya = { kind: "human" as const, id: "user_maya", name: "Maya" };
export const friday = { kind: "agent" as const, id: "friday", name: "Friday" };

export const approval: ActivityItem = {
  id: "paperclip:approval:a1", kind: "paperclip", subtype: "approval", roomId: null, messageId: null, threadRootMessageId: null,
  actor: growth, createdAt: "2026-09-08T10:00:00Z",
  state: "open", resolvedBy: null, resolvedAt: null, read: false,
  summary: {
    title: "Growth: Pilot X auto-replies", requester: growth, reason: null,
    preview: null, links: [{ kind: "issue", id: "issue-41", label: "LB-41" }],
  },
  actions: [
    { id: "approve", label: "Approve", style: "primary", confirm: null, input: null, navigate: null },
    { id: "reject", label: "Reject", style: "destructive", confirm: "Reject this?", input: null, navigate: null },
    { id: "open", label: "Open task", style: null, confirm: null, input: null, navigate: { kind: "issue", id: "issue-41", roomId: null } },
  ],
};

export const ask: ActivityItem = {
  ...approval, id: "paperclip:interaction:iss-4:int-1", subtype: "interaction",
  summary: { title: "Growth has a question for you", requester: growth, reason: "Which plan name?", preview: null, links: [] },
  actions: [
    { id: "answer", label: "Answer", style: "primary", confirm: null, navigate: null,
      input: { kind: "select", label: "Your answer", required: true, options: [{ value: "o1", label: "Pro" }, { value: "o2", label: "Team" }] } },
  ],
};

export function message(id: string, actor = maya, read = false, kind: ActivityItem["kind"] = "mention"): ActivityItem {
  return {
    ...approval, id: `${kind}:${id}`, kind, subtype: null, roomId: "room-1", messageId: id, read,
    actor, createdAt: "2026-09-08T11:00:00Z",
    summary: { title: `${actor.name} mentioned you in #launch`, requester: actor, reason: null,
      preview: { text: `message ${id}`, destination: null, fields: [] }, links: [{ kind: "room", id: "room-1", label: "#launch" }] },
    actions: [
      { id: "reply", label: "Reply", style: "primary", confirm: null, input: { kind: "text", label: "Reply", required: true, options: null }, navigate: null },
      { id: "open", label: "Open thread", style: null, confirm: null, input: null, navigate: { kind: "thread", id, roomId: "room-1" } },
    ],
  };
}

export const failedRun: ActivityItem = {
  ...approval, id: "paperclip:failed_run:r1", subtype: "failed_run", actor: friday,
  summary: { title: "Friday's last run failed", requester: friday, reason: "Timed out", preview: null, links: [{ kind: "run", id: "r1", label: "Run" }] },
  actions: [{ id: "open", label: "Open run", style: null, confirm: null, input: null, navigate: { kind: "run", id: "r1", roomId: null } }],
};

export const assigned: ActivityItem = {
  ...approval, id: "paperclip:assigned_issue:i9", subtype: "assigned_issue", actor: maya,
  summary: { title: "LB-9 is assigned to you: Send invoices", requester: maya, reason: "Status: Todo", preview: null, links: [{ kind: "issue", id: "i9", label: "LB-9" }] },
  actions: [{ id: "open", label: "Open issue", style: null, confirm: null, input: null, navigate: { kind: "issue", id: "i9", roomId: null } }],
};

export const loginAsk: ActivityItem = {
  ...approval, id: "login:abc", kind: "login", subtype: "login", actor: friday, roomId: "room-1",
  summary: { title: "Friday needs the login for supademo.com", requester: friday, reason: null, preview: null, links: [] },
  actions: [
    { id: "provide", label: "Add login", style: "primary", confirm: null, navigate: null,
      input: { kind: "login", label: "Login for supademo.com", required: true, options: null, hint: "Saved to the workspace's website logins." } },
  ],
};

export function attention(id: string, sourceKind: AttentionItem["sourceKind"], agent: string | null = "Finance"): AttentionItem {
  return {
    id, sourceKind, whyNow: "Waiting on you", severity: "medium",
    activityAt: "2026-09-08T09:00:00Z", createdAt: "2026-09-08T09:00:00Z",
    subject: { kind: sourceKind, id: `subject-${id}`, title: `Subject ${id}`, identifier: null, status: null, href: null },
    relatedIssue: { kind: "issue", id: `issue-${id}`, title: null, identifier: "LB-1", status: null, href: null },
    originAgentName: agent,
  };
}

export function post(seq: number, over: Partial<CanonicalRoomMessage> = {}): CanonicalRoomMessage {
  return {
    messageId: `post-${seq}`, roomId: "feed-1", messageSeq: seq, roomRevision: 1,
    author: { kind: "agent", id: "agent-fin", displayName: "Finance" },
    text: `**Post ${seq} title**\nBody of post ${seq}.`, mentions: [], replyToMessageId: null, threadRootMessageId: null,
    createdAt: "2026-10-15T09:00:00Z", runId: null, ...over,
  };
}
