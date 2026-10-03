import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import type {
  LocalSession,
  LocalSessionCommandReceipt,
  LocalSessionPickup,
  LocalSessionApprovalMode,
  LocalSessionAuditEntry,
  LocalSessionEvent,
  PrivateSendApproval,
} from "./types";
import type { createMessagingClient } from "./client";
import { AUTO_CHECK_INTERVAL_MS, receiptChip, type SessionCommandAttempt, type SessionReceiptChip } from "./useSessionCommand";

type Client = Pick<ReturnType<typeof createMessagingClient>,
  "submitLocalSessionCommand" | "cancelLocalSessionCommand" | "listLocalSessionOutbox" | "getPrivateSendApproval"
  | "setLocalSessionTeamControl" | "listLocalSessionAudit" | "setLocalSessionUsageSharing"
  | "setLocalSessionPickup" | "setLocalSessionSharing" | "requestLocalSessionJoin" | "removeLocalSessionSender"
  | "resumeLocalSession" | "revokeLocalSessionResume" | "getLocalSessionCommand" | "getLocalSession">;

/** A session client that may predate the control routes (test doubles, older callers). */
export type SessionControlClient = Pick<Client, "submitLocalSessionCommand"> & Partial<Omit<Client, "submitLocalSessionCommand">>;

export type { PrivateSendApproval };

/**
 * Who may use a session's controls (owner ruling 2026-09-25): its owner, any
 * workspace member while the owner has "Let teammates control this session" on,
 * and (2026-09-28) a person the owner allowed after they asked to join. `userId`
 * is the reader. The backend enforces the same rule; this only decides what to draw.
 */
export function canControlSession(session: LocalSession, own: boolean, userId?: string | null): boolean {
  return session.status !== "ended" &&
    (own || session.teamControl === true || (!!userId && (session.allowedSenders ?? []).includes(userId)));
}

/**
 * A teammate's "Ask to join": `state` is the server's answer (none, pending, declined),
 * held as pending the moment they tap. Asking again while pending is a no-op server-side.
 */
export function useSessionJoinRequest(client: SessionControlClient, session: LocalSession) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // This person's own reply, kept only until the next session update of any kind: whatever the
  // session says after that (the same request, an answer, an allow, a removal) is at least as new.
  const [override, setOverride] = useState<{ state: LocalSession["joinRequest"]; sent?: true; base?: LocalSession } | undefined>(undefined);
  const latest = useRef(session);
  latest.current = session;
  const mine = override !== undefined && (override.sent === true || override.base === session);
  const state = (mine ? override.state : session.joinRequest) ?? null;
  const ask = useCallback(async () => {
    if (!client.requestLocalSessionJoin) return;
    const started = latest.current;
    setSaving(true); setError(null); setOverride({ state: "pending", sent: true });
    try {
      const reply = await client.requestLocalSessionJoin(session.sessionId);
      // The session moved while this was in flight: whatever it says now is newer than the reply.
      setOverride(latest.current === started ? { state: reply.joinRequest ?? null, base: started } : undefined);
    }
    catch { setOverride(undefined); setError("Could not send your request. Try again."); }
    finally { setSaving(false); }
  }, [client, session.sessionId]);
  return { state, saving, error, ask, supported: !!client.requestLocalSessionJoin };
}

/** The one follow-up "Commit & open PR" sends. Plain instructions: the harness, not
 * Lightbulb, runs git, under the owner's own permissions and prompts. */
export const COMMIT_AND_PR_INSTRUCTION = [
  "Commit the changes you made in this session and open a pull request:",
  "1. Create a new branch from the current one (keep a sensible name).",
  "2. Stage only the files this session changed; leave unrelated changes alone.",
  "3. Commit with a clear message, push the branch, and open a pull request with a short summary of what changed and how it was tested.",
  "4. Reply with the pull request link. If anything blocks you (failing checks, a protected branch, missing credentials), stop and say what.",
].join("\n");

export const APPROVAL_MODE_LABEL: Record<LocalSessionApprovalMode, string> = {
  acceptEdits: "auto-accept edits",
  plan: "plan mode",
  default: "ask before edits",
};

const MODE_LABEL: Record<string, string> = {
  default: "Ask before edits", manual: "Ask before edits", acceptEdits: "Auto-accept edits", plan: "Plan mode",
  auto: "Auto mode", dontAsk: "Don't ask", bypassPermissions: "Bypass permissions",
};

/** "claude-opus-5-5 · Auto-accept edits": what the harness reports about its own run, or null. */
export function sessionFacts(session: Pick<LocalSession, "model" | "permissionMode">): string | null {
  const mode = session.permissionMode ? MODE_LABEL[session.permissionMode] ?? session.permissionMode : null;
  const parts = [session.model, mode].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

/** Claude Code's footer under its prompt, by permission mode; the default mode prints none. */
const CLAUDE_FOOTER: Record<string, string> = {
  acceptEdits: "⏵⏵ accept edits on", plan: "⏸ plan mode on", auto: "⏵⏵ auto mode on",
  bypassPermissions: "⏵⏵ bypass permissions on", dontAsk: "⏵⏵ don't ask on",
};

/**
 * The line each terminal keeps under its prompt: Claude Code's mode
 * ("⏵⏵ accept edits on") and model, Codex's model and folder. "" when the
 * tool has reported neither.
 */
export function sessionFooter(session: Pick<LocalSession, "provider" | "model" | "permissionMode" | "cwdName">): string {
  const parts = session.provider === "claude"
    ? [CLAUDE_FOOTER[session.permissionMode ?? ""], session.model]
    : [session.model, session.cwdName ? `~/${session.cwdName}` : ""];
  return parts.filter(Boolean).join(" · ");
}

const compact = (n: number) => n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);

/** "1.2k in · 340 out · 90.0k cached": the harness's own token counts. There is no cost:
 * neither CLI reports one, and a subscription seat has no per-token price to multiply by. */
export function usageLine(usage: LocalSession["usage"]): string | null {
  if (!usage) return null;
  const cached = usage.cacheReadTokens + usage.cacheWriteTokens;
  return [`${compact(usage.inputTokens)} in`, `${compact(usage.outputTokens)} out`, cached ? `${compact(cached)} cached` : ""]
    .filter(Boolean).join(" · ");
}

const ACTION_LINE: Record<LocalSessionAuditEntry["action"], string> = {
  native_send_requested: "recorded a pending native send",
  native_send_approved: "approved a native send",
  native_send_denied: "denied a native send",
  native_send_cancelled: "cancelled a pending native send",
  send: "sent a follow-up",
  cancel: "cancelled a queued follow-up",
  interrupt: "stopped the session",
  approve: "approved a permission prompt",
  deny: "denied a permission prompt",
  team_control_on: "let teammates control this session",
  team_control_off: "turned off teammate control",
  team_control_ended: "",
  share_link_created: "created a share link",
  share_link_revoked: "revoked a share link",
  pickup_on: "let teammates pick up this session",
  pickup_off: "turned off pick-up and deleted its snapshot",
  picked_up: "picked this session up on their Mac",
  pickup_expired: "",
  join_requested: "asked to join this session",
  join_allowed: "",
  join_declined: "",
  sender_removed: "",
  resume_requested: "asked to resume this session",
  resume_revoked: "",
};

/** Owner decisions about one person, by the person they were about. */
const SUBJECT_LINE: Partial<Record<LocalSessionAuditEntry["action"], (subject: string) => string>> = {
  join_allowed: subject => `let ${subject} send to this session`,
  join_declined: subject => `declined ${subject}'s request to join`,
  sender_removed: subject => `removed ${subject} from this session`,
  resume_revoked: subject => `stopped ${subject} from resuming this session`,
};

/** "Aden approved a permission prompt from Lightbulb" -- the transcript's audit line. */
export function auditLine(entry: LocalSessionAuditEntry, nameOf: (userId: string) => string): string {
  if (entry.action === "team_control_ended") return "Teammate control turned off when the session ended";
  if (entry.action === "pickup_expired") return "Pick-up turned off and its snapshot was deleted, 7 days after the session ended";
  const who = entry.actorUserId ? nameOf(entry.actorUserId) : entry.actorAgentId ? "An agent" : "Lightbulb";
  const failed = entry.state === "rejected" ? " (not delivered)" : entry.state === "unknown" ? " (delivery unknown)" : "";
  const subjectLine = SUBJECT_LINE[entry.action];
  if (subjectLine) return `${who} ${subjectLine(entry.subjectUserId ? nameOf(entry.subjectUserId) : "a teammate")}`;
  const where = /^(team_control|share_link|pickup|picked_up|join_|resume_)/.test(entry.action) ? "" : " from Lightbulb";
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

/** The longest prefix of `text` that fits `max` UTF-8 bytes, never splitting a character. */
function clipBytes(text: string, max: number): string {
  const encoder = new TextEncoder();
  let out = "";
  for (const ch of text) {
    if (encoder.encode(out + ch).length > max) break;
    out += ch;
  }
  return out;
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
    const text = decision === "deny" ? clipBytes(reason?.trim() ?? "", 500) : "";
    const key = `${pending.approvalId}:${decision}:${mode ?? ""}:${text}`;
    const requestId = ids.current.get(key) ?? uuid();
    ids.current.set(key, requestId);
    setState({ approvalId: pending.approvalId, status: "sending" });
    try {
      const receipt = await client.submitLocalSessionCommand(session.sessionId, {
        requestId, expectedGeneration: session.generation, kind: "approve", approvalId: pending.approvalId,
        inputDigest: pending.inputDigest, decision, ...(mode ? { mode } : {}),
        ...(text ? { text } : {}),
      });
      // Admitted is not delivered: a prompt answered in the terminal first comes back rejected.
      setState(receipt.state === "rejected"
        ? { approvalId: pending.approvalId, status: "failed", error: "Not delivered: the prompt was already answered or let go." }
        : { approvalId: pending.approvalId, status: "sent" });
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

/** The owner's "Show usage to teammates", with the server's answer as the truth. */
export function useUsageSharing(client: SessionControlClient, session: LocalSession) {
  const [saving, setSaving] = useState(false);
  const [override, setOverride] = useState<boolean | null>(null);
  useEffect(() => { setOverride(null); }, [session.shareUsage]);
  const set = useCallback(async (next: boolean) => {
    if (!client.setLocalSessionUsageSharing) return;
    setSaving(true);
    try { setOverride((await client.setLocalSessionUsageSharing(session.sessionId, next)).shareUsage === true); }
    catch { /* the checkbox stays where the server left it */ }
    finally { setSaving(false); }
  }, [client, session.sessionId]);
  return { enabled: override ?? session.shareUsage === true, saving, set };
}

/** One owner switch on a session: optimistic, with the server's answer as the truth. */
function useSessionSwitch(current: boolean, save: ((next: boolean) => Promise<boolean>) | null, failed: string) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [override, setOverride] = useState<boolean | null>(null);
  useEffect(() => { setOverride(null); }, [current]);
  const set = useCallback(async (next: boolean) => {
    if (!save) return;
    setSaving(true); setError(null); setOverride(next);
    try { setOverride(await save(next)); }
    catch { setOverride(null); setError(failed); }
    finally { setSaving(false); }
  }, [save, failed]);
  return { enabled: override ?? current, saving, error, set, supported: save !== null };
}

/** The owner's "Let teammates pick up this session". */
export function useSessionPickupPermission(client: SessionControlClient, session: LocalSession) {
  const { setLocalSessionPickup } = client;
  const save = useCallback(async (next: boolean) => (await setLocalSessionPickup!(session.sessionId, next)).pickup?.allowed === true,
    [setLocalSessionPickup, session.sessionId]);
  return useSessionSwitch(session.pickup?.allowed === true, setLocalSessionPickup ? save : null, "Could not change pick-up for this session.");
}

/** The owner's "Shared with the workspace": off pauses sharing, and only they can see the session. */
export function useSessionSharing(client: SessionControlClient, session: LocalSession) {
  const { setLocalSessionSharing } = client;
  const save = useCallback(async (next: boolean) => (await setLocalSessionSharing!(session.sessionId, next)).shared !== false,
    [setLocalSessionSharing, session.sessionId]);
  return useSessionSwitch(session.shared !== false, setLocalSessionSharing ? save : null, "Could not change sharing. Try again.");
}

const megabytes = (bytes: number) => (bytes >= 1_048_576 ? `${(bytes / 1_048_576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`);

/** "github.com/acme/app on fix/login", "acme/app", or "" when the snapshot holds no files. */
export function pickupSource(pickup: LocalSessionPickup | undefined): string {
  if (!pickup?.hasCode || !pickup.repo) return "";
  const repo = pickup.repo.split("/").slice(-2).join("/");
  return pickup.branch ? `${repo} on ${pickup.branch}` : repo;
}

/**
 * Where a session's snapshot stands, in one sentence for the person deciding
 * whether to pick it up. `ago` formats the snapshot's age ("2m ago").
 */
export function pickupStatus(pickup: LocalSessionPickup | undefined, ago: (iso: string) => string): string {
  if (!pickup?.allowed) return "";
  if (pickup.state === "unavailable") return UNAVAILABLE[pickup.reason ?? ""] ?? "The owner's Mac could not take a snapshot of this session.";
  if (pickup.state === "preparing") return "Taking the first snapshot. It uploads when the current turn finishes.";
  const source = pickupSource(pickup);
  return [
    pickup.snapshotAt ? `Snapshot taken ${ago(pickup.snapshotAt)}` : "Snapshot ready",
    pickup.bytes ? megabytes(pickup.bytes) : "",
    source ? `files from ${source}` : "conversation only, no files",
    // Set once the session has stopped: `ago` reads a future time as "in 5d".
    pickup.expiresAt ? `deleted ${ago(pickup.expiresAt)}` : "",
  ].filter(Boolean).join(" · ");
}

const UNAVAILABLE: Record<string, string> = {
  "transcript-missing": "This session's history is no longer on the owner's Mac.",
  "too-large": "This session is too large to snapshot.",
  "unsupported-version": "The owner's Claude Code is too old to be picked up from.",
};

/** An ApiProblem body's own `message`, when it carries one. */
function problemMessage(body: unknown): string | null {
  const m = (body as { message?: unknown } | null)?.message;
  return typeof m === "string" && m ? m : null;
}

/**
 * "Resume and send" (spec §5.7): an ended session, for its owner and for the people who could send
 * to it when it ended, while no other resume waits. The backend enforces the same rule.
 */
export function canResumeSession(session: LocalSession, own: boolean, userId?: string | null): boolean {
  return session.resumable === true && session.status === "ended" && session.kind !== "background" && session.resume?.state !== "pending" &&
    (own || (!!userId && (session.resumeAllowed ?? []).includes(userId)));
}

const RESUME_REFUSED: Record<string, (mac: string, folder: string) => string> = {
  ALREADY_RUNNING: mac => `it is already running on ${mac}`,
  FOLDER_NOT_APPROVED: (mac, folder) => `${folder} is no longer an approved folder on ${mac}`,
  FOLDER_MISSING: mac => `its folder is no longer on ${mac}`,
  TOOL_UNAVAILABLE: mac => `its CLI is no longer installed on ${mac}`,
  SHARING_OFF: mac => `session sharing is off on ${mac}`,
};

/** One line about a resume already asked for (waiting, or refused by the Mac), or null. */
export function resumeStatus(session: LocalSession, own: boolean, ownerName: string, nameOf: (userId: string) => string, userId?: string | null): string | null {
  const resume = session.resume;
  if (!resume) return null;
  const mac = own ? "your Mac" : `${ownerName}’s Mac`;
  if (resume.state === "failed") return `Not resumed: ${(RESUME_REFUSED[resume.reason ?? ""] ?? ((m: string) => `${m} could not start it`))(mac, session.cwdName || "its folder")}.`;
  const who = resume.byUserId === userId ? "You" : nameOf(resume.byUserId);
  return `${who} asked to resume this session. It starts on ${mac} when that Mac is on, within 24 hours.`;
}

export type SessionResumeFlow = {
  available: boolean; status: string | null; saving: boolean; error: string | null;
  resume: (message: string) => Promise<boolean>;
  /** Where the first message stands once the session runs again; null before, and once it landed. */
  chip: SessionReceiptChip | null;
};

/** The first message of each resume this page asked for, by session: the view that shows its
 * delivery may remount when the session starts running again. */
const firstMessages = new Map<string, { requestId: string; text: string }>();

export function useSessionResume(client: SessionControlClient, prop: LocalSession, opts: {
  own: boolean; userId?: string | null; ownerName: string; nameOf: (userId: string) => string; uuid: () => string;
}): SessionResumeFlow {
  const { own, userId, ownerName, nameOf, uuid } = opts;
  // Ended sessions are absent from workspace listings, so the page's copy goes stale once a resume
  // is asked or refused: keep the newer read while this is still the same ended run.
  const [fresh, setFresh] = useState<LocalSession | null>(null);
  const session = fresh && fresh.sessionId === prop.sessionId && fresh.generation === prop.generation && prop.status === "ended" ? fresh : prop;
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [first, setFirst] = useState(() => firstMessages.get(session.sessionId) ?? null);
  const [receipt, setReceipt] = useState<LocalSessionCommandReceipt | null>(null);
  const [check, setCheck] = useState<"waiting" | "missing" | "uncertain">("waiting");
  // One id per attempt: a retry after a lost reply is the same request, never a second resume.
  const attemptId = useRef<string | null>(null);
  const running = session.status !== "ended";
  const resume = useCallback(async (message: string) => {
    if (!client.resumeLocalSession) return false;
    const resumeId = attemptId.current ?? uuid();
    attemptId.current = resumeId;
    const text = message.trim();
    setSaving(true); setError(null);
    try {
      const updated = await client.resumeLocalSession(session.sessionId, { resumeId, ...(text ? { message: text } : {}) });
      attemptId.current = null;
      if (updated?.status === "ended") setFresh(updated);
      if (text) {
        const sent = { requestId: resumeId, text };
        firstMessages.set(session.sessionId, sent);
        setFirst(sent); setReceipt(null); setCheck("waiting");
      }
      return true;
    } catch (e) {
      // A refusal is final for this id; only an unanswered request is retried as itself.
      if (e instanceof ApiError && e.status < 500) attemptId.current = null;
      setError(!(e instanceof ApiError) ? "Could not reach Lightbulb. Try again."
        : e.status === 409 ? problemMessage(e.body) ?? "Someone already asked to resume this session, or it is running again."
          : e.status === 403 ? "You can no longer resume this session."
            : e.status === 400 || e.status === 422 ? "That message is too long."
              : "Could not resume this session. Try again.");
      return false;
    } finally { setSaving(false); }
  }, [client, session.sessionId, uuid]);
  const pending = session.status === "ended" && session.resume?.state === "pending";
  useEffect(() => {
    const read = client.getLocalSession;
    if (!pending || !read) return;
    let alive = true;
    let tries = 0;
    const tick = async () => {
      tries += 1;
      try {
        const latest = await read(session.sessionId);
        if (!alive) return;
        if (latest.status === "ended") setFresh(latest);
        if (latest.status !== "ended" || latest.resume?.state !== "pending") clearInterval(timer);
      } catch { /* the next tick asks again */ }
      if (tries >= 15) clearInterval(timer);
    };
    const timer = setInterval(() => void tick(), AUTO_CHECK_INTERVAL_MS);
    return () => { alive = false; clearInterval(timer); };
  }, [client, session.sessionId, pending]);
  useEffect(() => {
    const get = client.getLocalSessionCommand;
    if (!first || !running || !get) return;
    let alive = true;
    let tries = 0;
    const tick = async () => {
      tries += 1;
      try {
        const latest = await get(session.sessionId, first.requestId);
        if (!alive) return;
        setReceipt(latest); setCheck("waiting");
        if (latest.state !== "pending" && latest.state !== "dispatched") { clearInterval(timer); firstMessages.delete(session.sessionId); }
      } catch (e) {
        if (alive) setCheck(e instanceof ApiError && e.status === 404 ? "missing" : "uncertain");
      }
      if (tries >= 15) clearInterval(timer);
    };
    const timer = setInterval(() => void tick(), AUTO_CHECK_INTERVAL_MS);
    void tick();
    return () => { alive = false; clearInterval(timer); };
  }, [client, session.sessionId, running, first]);
  const attempt: SessionCommandAttempt | null = first && running ? {
    request: { requestId: first.requestId, expectedGeneration: session.generation, kind: "send", text: first.text },
    state: receipt ? "received" : check === "missing" ? "rejected" : check === "uncertain" ? "uncertain" : "sending",
    ...(receipt ? { receipt } : {}),
    ...(check === "missing" && !receipt ? { error: "The session started again without it." } : {}),
  } : null;
  return {
    available: !!client.resumeLocalSession && canResumeSession(session, own, userId),
    status: resumeStatus(session, own, ownerName, nameOf, userId),
    saving, error, resume, chip: receiptChip(attempt),
  };
}
