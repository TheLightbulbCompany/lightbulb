import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import type {
  LocalSession,
  LocalSessionApprovalMode,
  LocalSessionAuditEntry,
  LocalSessionEvent,
} from "./types";
import type { createMessagingClient } from "./client";

type Client = Pick<ReturnType<typeof createMessagingClient>,
  "submitLocalSessionCommand" | "setLocalSessionTeamControl" | "listLocalSessionAudit">;

/** A session client that may predate the control routes (test doubles, older callers). */
export type SessionControlClient = Pick<Client, "submitLocalSessionCommand"> & Partial<Omit<Client, "submitLocalSessionCommand">>;

/**
 * Who may use a session's controls (owner ruling 2026-09-25): its owner, and any
 * workspace member while the owner has "Let teammates control this session" on.
 * The backend enforces the same rule; this only decides what to draw.
 */
export function canControlSession(session: LocalSession, own: boolean): boolean {
  return session.status !== "ended" && (own || session.teamControl === true);
}

export const APPROVAL_MODE_LABEL: Record<LocalSessionApprovalMode, string> = {
  acceptEdits: "auto-accept edits",
  plan: "plan mode",
  default: "ask before edits",
};

const ACTION_LINE: Record<LocalSessionAuditEntry["action"], string> = {
  send: "sent a follow-up",
  interrupt: "stopped the session",
  approve: "approved a permission prompt",
  deny: "denied a permission prompt",
  team_control_on: "let teammates control this session",
  team_control_off: "turned off teammate control",
  team_control_ended: "",
  share_link_created: "created a share link",
  share_link_revoked: "revoked a share link",
};

/** "Aden approved a permission prompt from Lightbulb" -- the transcript's audit line. */
export function auditLine(entry: LocalSessionAuditEntry, nameOf: (userId: string) => string): string {
  if (entry.action === "team_control_ended") return "Teammate control turned off when the session ended";
  const who = entry.actorUserId ? nameOf(entry.actorUserId) : entry.actorAgentId ? "An agent" : "Lightbulb";
  const failed = entry.state === "rejected" ? " (not delivered)" : entry.state === "unknown" ? " (delivery unknown)" : "";
  const where = entry.action.startsWith("team_control") || entry.action.startsWith("share_link") ? "" : " from Lightbulb";
  return `${who} ${ACTION_LINE[entry.action]}${where}${failed}`;
}

/**
 * Audit entries woven into the transcript as notice rows, each placed after the
 * last loaded event at or before it. Follow-ups are left out: the prompt itself
 * already shows who sent it. Entries older than the loaded window stay out, so
 * paging back never reorders what is on screen.
 */
export function withAuditNotices(
  events: readonly LocalSessionEvent[], entries: readonly LocalSessionAuditEntry[], nameOf: (userId: string) => string,
): LocalSessionEvent[] {
  // Each event's time is its own ts, else the last one before it.
  let at = Number.NEGATIVE_INFINITY;
  const times = events.map(event => {
    const t = event.ts ? Date.parse(event.ts) : Number.NaN;
    if (Number.isFinite(t)) at = t;
    return at;
  });
  const first = times.find(Number.isFinite) ?? Number.NEGATIVE_INFINITY;
  const pending = entries
    .filter(e => e.action !== "send" && Date.parse(e.at) >= first)
    .sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  const notice = (entry: LocalSessionAuditEntry, anchor: number, index: number): LocalSessionEvent => ({
    seq: anchor + (index + 1) / 1000, kind: "text", role: "system", truncated: false, ts: entry.at, text: auditLine(entry, nameOf),
  });
  const out: LocalSessionEvent[] = [];
  let next = 0;
  events.forEach((event, i) => {
    out.push(event);
    const until = times[i + 1] ?? Number.POSITIVE_INFINITY;
    for (let index = 0; next < pending.length && Date.parse(pending[next].at) < until; index += 1, next += 1) {
      out.push(notice(pending[next], event.seq, index));
    }
  });
  pending.slice(next).forEach((entry, index) => out.push(notice(entry, 0, index)));
  return out;
}

/** The session's audit trail, refetched whenever the session or its transcript moves. */
export function useSessionAudit(client: SessionControlClient, sessionId: string, revision: unknown) {
  const [entries, setEntries] = useState<LocalSessionAuditEntry[]>([]);
  useEffect(() => {
    let alive = true;
    void client.listLocalSessionAudit?.(sessionId).then(page => { if (alive) setEntries(page.entries); }).catch(() => {});
    return () => { alive = false; };
  }, [client, sessionId, revision]);
  return entries;
}

export type ApprovalAnswer = { decision: "allow" | "deny"; mode?: LocalSessionApprovalMode; reason?: string };

/**
 * Answer the prompt a session holds, once. The request id is minted per prompt
 * and answer, so a retry of the same click is idempotent and never a second answer.
 */
export function useSessionApproval(client: Pick<Client, "submitLocalSessionCommand">, session: LocalSession, uuid: () => string) {
  const [state, setState] = useState<{ approvalId: string; status: "sending" | "sent" | "failed"; error?: string } | null>(null);
  const ids = useRef(new Map<string, string>());
  const pending = session.pendingApproval;
  const answer = useCallback(async ({ decision, mode, reason }: ApprovalAnswer) => {
    if (!pending) return;
    const key = `${pending.approvalId}:${decision}:${mode ?? ""}`;
    const requestId = ids.current.get(key) ?? uuid();
    ids.current.set(key, requestId);
    setState({ approvalId: pending.approvalId, status: "sending" });
    try {
      await client.submitLocalSessionCommand(session.sessionId, {
        requestId, expectedGeneration: session.generation, kind: "approve", approvalId: pending.approvalId,
        inputDigest: pending.inputDigest, decision, ...(mode ? { mode } : {}),
        ...(decision === "deny" && reason?.trim() ? { text: reason.trim().slice(0, 500) } : {}),
      });
      setState({ approvalId: pending.approvalId, status: "sent" });
    } catch (e) {
      const conflict = e instanceof ApiError && e.status === 409;
      setState({ approvalId: pending.approvalId, status: "failed", error: conflict
        ? "That prompt was already answered or has changed."
        : e instanceof ApiError && e.status === 403 ? "You can no longer control this session."
          : "Could not send the answer. Try again." });
    }
  }, [client, pending, session.sessionId, session.generation, uuid]);
  const current = state && pending && state.approvalId === pending.approvalId ? state : null;
  return { pending, answer, state: current };
}

/** The owner's toggle, with the server's answer as the truth. */
export function useTeamControl(client: SessionControlClient, session: LocalSession) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [override, setOverride] = useState<boolean | null>(null);
  useEffect(() => { setOverride(null); }, [session.teamControl]);
  const enabled = override ?? session.teamControl === true;
  const set = useCallback(async (next: boolean) => {
    setSaving(true); setError(null);
    try {
      if (!client.setLocalSessionTeamControl) throw new Error("unsupported");
      const updated = await client.setLocalSessionTeamControl(session.sessionId, next);
      setOverride(updated.teamControl === true);
    } catch {
      setError("Could not change who controls this session.");
    } finally { setSaving(false); }
  }, [client, session.sessionId]);
  return { enabled, saving, error, set };
}
