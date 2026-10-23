/**
 * Lightbulb Connect holds, answered from the phone and the web. A hold is a teammate's
 * first message (or an agent's permission prompt) that a Mac is keeping until its person
 * answers. A hold carries the sender, the target and the computer, never message text.
 */
import type { ApiMethods } from "../api/client";

export type HoldScope = "once" | "person" | "own_cloud";
export type HoldVerdict = "accept" | "refuse";

/** One row of `GET /network/holds` (the hold notice minus any preview). */
export interface NetworkHold {
  hold_id: string;
  holding_member: string;
  holding_computer: string;
  target_session: string;
  from: string;
  label: string;
  kind: "message" | "permission";
  created_at: string;
  /** The target session's agent display name; the session name stands in without it. */
  agent?: string;
  /** Permission holds: the prompt's tool name, at most 60 chars, never the command text. */
  title?: string;
  /** The sender is one of this workspace's agents: only then is Always allow (`own_cloud`) offered. */
  own_cloud?: boolean;
}

export interface HoldAnswer { id: string; label: string; verdict: HoldVerdict; scope: HoldScope }

export const HOLDS_PATH = "/network/holds";
export const APPROVALS_PATH = "/network/approvals";
export const APPROVALS_LABEL = "Answer Allow requests here";

/** The hold's one line (Task 11's push and Needs-you copy). */
export function holdText(hold: NetworkHold): string {
  const agent = hold.agent || hold.target_session;
  return hold.kind === "permission"
    ? `${agent} on ${hold.holding_computer} asks permission: ${(hold.title ?? "").slice(0, 60)}.`
    : `${hold.label} wants to message your ${agent} session on ${hold.holding_computer}.`;
}

/** What a hold offers: a permission hold only once or decline; Always allow only for this workspace's agents. */
export function holdAnswers(hold: NetworkHold): HoldAnswer[] {
  const once: HoldAnswer = { id: "once", label: "Allow once", verdict: "accept", scope: "once" };
  const decline: HoldAnswer = { id: "decline", label: "Decline", verdict: "refuse", scope: "once" };
  if (hold.kind === "permission") return [once, decline];
  return [
    once,
    { id: "person", label: `Allow ${hold.label} always`, verdict: "accept", scope: "person" },
    ...(hold.own_cloud ? [{ id: "own_cloud", label: "Always allow", verdict: "accept", scope: "own_cloud" } as HoldAnswer] : []),
    decline,
  ];
}

/** Posts the answer and returns the result line: `Allowed.`, `Declined.` or `Answered on your Mac.` */
export async function answerHold(api: Pick<ApiMethods, "post">, hold: NetworkHold, answer: HoldAnswer): Promise<string> {
  const body = (await api.post(`${HOLDS_PATH}/${encodeURIComponent(hold.hold_id)}/decision`, {
    holding_member: hold.holding_member, verdict: answer.verdict, scope: answer.scope,
  })) as { result?: string } | null;
  if (body?.result === "not_open") return "Answered on your Mac.";
  return answer.verdict === "accept" ? "Allowed." : "Declined.";
}

/** `GET /network/holds` → the open holds (a bare list or `{holds}`). */
export async function listHolds(api: Pick<ApiMethods, "get">): Promise<NetworkHold[]> {
  const body = (await api.get(HOLDS_PATH)) as NetworkHold[] | { holds?: NetworkHold[] } | null;
  return Array.isArray(body) ? body : body?.holds ?? [];
}

/** The settings switch: `approvals_linked` from `GET /network/workspace`; on writes the link, off removes it. */
export async function approvalsLinked(api: Pick<ApiMethods, "get">): Promise<boolean> {
  const body = (await api.get("/network/workspace")) as { approvals_linked?: boolean } | null;
  return Boolean(body?.approvals_linked);
}

export async function setApprovalsLinked(api: Pick<ApiMethods, "post" | "del">, on: boolean): Promise<void> {
  if (on) await api.post(APPROVALS_PATH, {});
  else await api.del(APPROVALS_PATH);
}
