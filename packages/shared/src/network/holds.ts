/**
 * Lightbulb Connect holds, answered from the phone and the web. A hold is a teammate's
 * first message (or an agent's permission prompt) that a Mac is keeping until its person
 * answers. A hold carries the sender, the target and the computer, never message text.
 */
import { useCallback, useRef, useState } from "react";
import useSWR from "swr";
import type { ApiMethods } from "../api/client";
import { ApiError } from "../api/error";
import type { ActivityItem } from "../messaging/types";

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
export const APPROVALS_DESCRIPTION =
  "When a teammate's agent first messages one of your sessions, your Mac asks you. Turn this on to answer here and on your phone too.";
export const APPROVALS_SAVE_FAILED = "Couldn’t save this setting. Try again.";
export const HOLD_FAILED = "That didn’t go through. Try again.";

/** The push `kind`, the activity item kind and the focus prefix of a hold. */
export const HOLD_KIND = "network_hold";
/** The id a hold's row / card carries, and the `focus` a hold push lands with. */
export const holdFocusId = (holdId: string): string => `${HOLD_KIND}:${holdId}`;
export const isHoldFocus = (focus: string | null | undefined): focus is string => Boolean(focus?.startsWith(`${HOLD_KIND}:`));
/** A hold's feed item: both clients drop it and answer the hold from `/network/holds` instead. */
export const isHoldFeedItem = (item: Pick<ActivityItem, "kind">): boolean => item.kind === HOLD_KIND;

/** The hold's one line (Task 11's push and Needs-you copy). */
export function holdText(hold: NetworkHold): string {
  const agent = hold.agent || hold.target_session;
  const title = (hold.title ?? "").slice(0, 60);
  return hold.kind === "permission"
    ? `${agent} on ${hold.holding_computer} asks permission${title ? `: ${title}` : ""}.`
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
  let body: { result?: string } | null;
  try {
    body = (await api.post(`${HOLDS_PATH}/${encodeURIComponent(hold.hold_id)}/decision`, {
      holding_member: hold.holding_member, verdict: answer.verdict, scope: answer.scope,
    })) as { result?: string } | null;
  } catch (err) {
    // The hold is no longer open here (answered elsewhere, or expired): a retry can never land.
    if (err instanceof ApiError && err.status === 404) return "Answered on your Mac.";
    throw err;
  }
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

/**
 * The approvals switch, for both clients: loads once, one write at a time (later taps are
 * ignored until it settles), and a failed write leaves `linked` at the server's value, sets
 * `error`, and bumps `epoch` so a toggle that keeps its own state (SwiftUI) can remount.
 * `linked` is null until loaded, and stays null without a connected workspace.
 */
export function useApprovalsLink(api: Pick<ApiMethods, "get" | "post" | "del">) {
  const { data, mutate } = useSWR("/network/workspace#approvals", () => approvalsLinked(api), { revalidateOnFocus: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [epoch, setEpoch] = useState(0);
  const inFlight = useRef(false);
  const change = useCallback((next: boolean) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    setApprovalsLinked(api, next)
      .then(() => mutate(next, { revalidate: false }))
      .catch(() => {
        setError(APPROVALS_SAVE_FAILED);
        setEpoch((value) => value + 1);
      })
      .finally(() => {
        inFlight.current = false;
        setBusy(false);
      });
  }, [api, mutate]);
  return { linked: data ?? null, busy, error, epoch, change };
}
