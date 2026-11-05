// The mockup walkthrough (m0/mockup.html) as API data, so web and mobile build before the routes exist.
// `viewAs` plays the server's `summarize` for a second viewer (Priya) — tests only, never product code.

import type { CanonicalRoomMessage } from "../messaging/types";
import type { QuestionView, TaskDetail, TaskQuestionRow, TaskSummary } from "./types";

export const SAM = "user_sam";
export const PRIYA = "user_priya";
export const ROOM = "room-workshop-launch";
export const LIGHTBULB = "agent_ceo";

const AT = "2026-11-04T14:00:00.000Z";

export function fixtureTask(over: Partial<TaskSummary> & Pick<TaskSummary, "taskId" | "title">): TaskSummary {
  return {
    identifier: over.taskId.toUpperCase(),
    status: "working",
    statusReason: null,
    shownGroup: "working",
    specialist: null,
    ownerUserId: SAM,
    audience: { kind: "room", userIds: [], roomId: ROOM },
    audienceKind: "room",
    originRoomId: ROOM,
    taskRoomId: `task-room-${over.taskId}`,
    proposalReason: null,
    openQuestion: null,
    landing: null,
    output: null,
    watchUntil: null,
    watching: false,
    runsOn: "workspace",
    activity: { n: 1, status: "running", outcome: null, lastHeartbeatAt: AT },
    recentInputs: [],
    attemptCount: 1,
    usage: null,
    costUsd: 0,
    budgetUsd: null,
    updatedAt: AT,
    version: 1,
    ...over,
  };
}

/** The same task as another viewer sees it (mirrors `tasks_access._shown_group` for an open question). */
export function viewAs(t: TaskSummary, viewerId: string): TaskSummary {
  const q = t.openQuestion;
  if (!q) {
    if (t.status === "failed") return { ...t, shownGroup: t.ownerUserId === viewerId ? "waiting_on_you" : "idle" };
    return t;
  }
  const canAnswer = q.answerers.includes(viewerId);
  const shownGroup = canAnswer ? (q.kind === "review" ? "ready_for_review" : "waiting_on_you")
    : t.landing ? "landing" : "working";
  return { ...t, shownGroup, openQuestion: { ...q, canAnswer } };
}

export function fixtureMessage(over: Partial<CanonicalRoomMessage> & Pick<CanonicalRoomMessage, "messageId" | "text">): CanonicalRoomMessage {
  return {
    roomId: ROOM,
    messageSeq: 1,
    roomRevision: 1,
    author: { kind: "agent", id: LIGHTBULB, displayName: "Lightbulb" },
    mentions: [],
    replyToMessageId: null,
    threadRootMessageId: null,
    createdAt: AT,
    runId: null,
    ...over,
  };
}

const signup = fixtureTask({ taskId: "t-signup", title: "Workshop signup app", specialist: "engineering" });
const email = fixtureTask({ taskId: "t-email", title: "Launch email draft", specialist: "marketing" });
const fees = fixtureTask({
  taskId: "t-fees", title: "Stripe fee estimate", specialist: "finance",
  status: "proposed", shownGroup: null, activity: null, attemptCount: 0, proposalReason: "would use a paid tool",
});

const ask = fixtureMessage({
  messageId: "m-ask", messageSeq: 1,
  author: { kind: "human", id: SAM, displayName: "Sam" },
  text: "@lightbulb build a signup page for the workshop, draft the launch email, and estimate Stripe fees",
  routedTo: { taskIds: ["t-signup", "t-email", "t-fees"], started: 2, suggested: 1, passThrough: false },
});
const reply = fixtureMessage({
  messageId: "m-reply", messageSeq: 2, text: "On it: two threads started, one suggested.",
  taskRefs: [
    { taskId: "t-signup", role: "started" },
    { taskId: "t-email", role: "started" },
    { taskId: "t-fees", role: "suggested" },
  ],
});

const approval = {
  qid: "q-provision", kind: "approval" as const, subkind: null,
  title: "Provision database and Stripe", answerers: [SAM], answererNames: ["Sam"], canAnswer: true,
  options: [{ id: "deny", label: "Deny" }, { id: "approve", label: "Approve" }],
};
const signupWaiting = { ...signup, status: "waiting" as const, shownGroup: "waiting_on_you" as const, openQuestion: approval, version: 3 };
const feesWorking = { ...fees, status: "working" as const, shownGroup: "working" as const, version: 2, activity: signup.activity };
const feesDone = { ...feesWorking, status: "idle" as const, shownGroup: "idle" as const, version: 4, costUsd: 0.31 };
const emailReview = {
  ...email, status: "waiting" as const, shownGroup: "ready_for_review" as const, version: 4,
  openQuestion: {
    qid: "q-email", kind: "review" as const, subkind: null, title: "Send the launch email?",
    answerers: [SAM], answererNames: ["Sam"], canAnswer: true,
    options: [{ id: "request_changes", label: "Request changes" }, { id: "send", label: "Send" }],
  },
  output: { idemKey: "e-mail", kind: "mail" as const, title: "Draft: \"Join us Nov 20\" (240 words)", nextStep: "send" },
};
const signupWorking = { ...signup, version: 5, attemptCount: 2, activity: { n: 2, status: "running" as const, outcome: null, lastHeartbeatAt: AT }, costUsd: 0.84, budgetUsd: 5 };

const result = fixtureMessage({
  messageId: "m-fees-result", messageSeq: 5, specialist: "finance", kind: "result",
  text: "Stripe fees for 40 paid seats at $25: about $39.00 (2.9% + 30¢ each).",
  taskRefs: [{ taskId: "t-fees", role: "started" }],
});

/** The provision approval after Sam approved it: the row (`GET /tasks/{id}`) and Sam's view (`GET /tasks/{id}/questions`). */
export const answeredApprovalRow: TaskQuestionRow = {
  qid: "q-provision", kind: "approval", subkind: null, title: "Provision database and Stripe",
  text: "Allow Lightbulb · Engineering to provision a database and Stripe payments?",
  options: [{ id: "deny", label: "Deny" }, { id: "approve", label: "Approve" }],
  answerers: [SAM], state: "answered", answer: { optionId: "approve", text: null }, answeredBy: SAM,
  answeredAt: "2026-11-04T14:02:00.000Z", createdAt: AT, version: 2,
};
export const answeredApprovalView: QuestionView = {
  id: "q-provision", taskId: "t-signup", kind: "approval", subkind: null, title: answeredApprovalRow.title, text: answeredApprovalRow.text!,
  options: [{ id: "deny", label: "Deny", style: "danger" }, { id: "approve", label: "Approve", style: "primary" }],
  state: "answered", answerers: [{ id: SAM, name: "Sam" }], canAnswer: false, waitingLabel: null,
  answer: { optionId: "approve", text: null }, answeredBy: { id: SAM, name: "Sam" }, answeredAt: answeredApprovalRow.answeredAt!,
  createdAt: AT, version: 2,
};

export interface FixtureStep { messages: CanonicalRoomMessage[]; tasks: TaskSummary[]; details: Record<string, TaskDetail> }

const detail = (task: TaskSummary, questions: TaskQuestionRow[] = []): TaskDetail => ({
  task,
  attempts: Array.from({ length: Math.max(task.attemptCount ?? 0, 0) }, (_, i) => ({
    n: i + 1, status: i + 1 === task.attemptCount ? "running" : "ended", outcome: i + 1 === task.attemptCount ? null : "waiting_approval",
    startedAt: AT, endedAt: i + 1 === task.attemptCount ? null : AT,
  })),
  questions,
  effects: [],
  subscriptions: [{ threadKey: `room:${ROOM}:m-reply`, createdBy: "router", active: true }],
  taskRoomId: task.taskRoomId,
});

const step = (messages: CanonicalRoomMessage[], tasks: TaskSummary[], details: TaskDetail[] = []): FixtureStep =>
  ({ messages, tasks, details: Object.fromEntries(details.map((d) => [d.task.taskId, d])) });

/** As Sam sees them. Steps follow m0/mockup.html. */
export const FIXTURES = {
  step02: step([ask, reply], [signup, email, fees]),
  step03: step([ask, reply], [signup, email, feesWorking]),
  step05: step([ask, reply], [signupWaiting, email, feesWorking]),
  step07: step([ask, reply, result], [signupWorking, emailReview, feesDone]),
  step08: step([ask, reply, result], [signupWorking, emailReview, feesDone], [detail(signupWorking, [answeredApprovalRow])]),
  step09: step([ask, reply, result], [
    { ...signupWorking, status: "resolved", shownGroup: "resolved", version: 7, activity: null },
    { ...emailReview, status: "working", shownGroup: "landing", landing: "e-mail", openQuestion: null, version: 5 },
    feesDone,
  ]),
} satisfies Record<string, FixtureStep>;
