import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import type {
  LocalSession,
  LocalSessionCommand,
  LocalSessionCommandReceipt,
  LocalSessionPickup,
  LocalSessionApprovalMode,
  LocalSessionAuditEntry,
  LocalSessionEvent,
  LocalSessionPendingApproval,
} from "./types";
import type { createMessagingClient } from "./client";
import { canonicalJson, hasHiddenChars } from "./localSessionTranscript";
import { AUTO_CHECK_INTERVAL_MS, receiptChip, type SessionCommandAttempt, type SessionReceiptChip } from "./useSessionCommand";

type Client = Pick<ReturnType<typeof createMessagingClient>,
  "submitLocalSessionCommand" | "cancelLocalSessionCommand" | "listLocalSessionOutbox"
  | "setLocalSessionTeamControl" | "listLocalSessionAudit" | "setLocalSessionUsageSharing"
  | "setLocalSessionSharing" | "requestLocalSessionJoin" | "requestLocalSessionPickupAccess" | "removeLocalSessionSender"
  | "resumeLocalSession" | "revokeLocalSessionResume" | "getLocalSessionCommand" | "getLocalSession">;

/** A session client that may predate the control routes (test doubles, older callers). */
export type SessionControlClient = Pick<Client, "submitLocalSessionCommand"> & Partial<Omit<Client, "submitLocalSessionCommand">>;

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

/** The owner's row for a pick-up request, web and phone alike. It says what Allow shares,
 * in the substance of the owner's own pick-up switch warning. */
export const pickupRequestLine = (name: string) =>
  `${name} wants to pick up this session. Allow lets everyone in the workspace pick it up, and uploads the whole conversation and your uncommitted changes as they are. Nothing is redacted.`;

/** How long a request waits before its sender may remind the owner; the backend's ASK_AGAIN_AFTER_S. */
export const ASK_AGAIN_AFTER_MS = 10 * 60_000;

/**
 * A teammate's "Ask to join": `state` is the server's answer (none, pending, declined),
 * held as pending the moment they tap. A request never times out: `again` turns true once it has
 * waited ASK_AGAIN_AFTER_MS, and asking then reminds the owner. `lapsed`: the server says this
 * person's pending request was dropped unanswered when the session ended or restarted
 * (`joinRequestClosed`), so they are told instead of the button silently resetting.
 */
export function useSessionJoinRequest(client: SessionControlClient, session: LocalSession, kind: "join" | "pickup" = "join") {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // This person's own reply, kept only until the next session update of any kind: whatever the
  // session says after that (the same request, an answer, an allow, a removal) is at least as new.
  const [override, setOverride] = useState<{ state: LocalSession["joinRequest"] | LocalSession["pickupRequest"]; sent?: true; base?: LocalSession } | undefined>(undefined);
  const latest = useRef(session);
  latest.current = session;
  const mine = override !== undefined && (override.sent === true || override.base === session);
  const field = kind === "pickup" ? "pickupRequest" : "joinRequest";
  const send = kind === "pickup" ? client.requestLocalSessionPickupAccess : client.requestLocalSessionJoin;
  const state = (mine ? override.state : session[field]) ?? null;
  const askedAt = session[kind === "pickup" ? "pickupRequestedAt" : "joinRequestedAt"];
  // This tap, until the session says when it landed: Ask again does not stay offered after it is used.
  const [sentAt, setSentAt] = useState(0);
  const dueAt = state === "pending" && askedAt ? Math.max(Date.parse(askedAt), sentAt) + ASK_AGAIN_AFTER_MS : 0;
  const [, redraw] = useState(0);
  useEffect(() => {
    const left = dueAt - Date.now();
    if (!(left > 0)) return;
    const timer = setTimeout(() => redraw(n => n + 1), left);
    return () => clearTimeout(timer);
  }, [dueAt]);
  const again = dueAt > 0 && Date.now() >= dueAt;
  // The server's word: both session views remount on a new generation, so nothing held here survives the restart.
  const lapsed = kind === "join" && state === null && session.joinRequestClosed === "restarted";
  const ask = useCallback(async () => {
    if (!send) return;
    const started = latest.current;
    setSaving(true); setError(null); setOverride({ state: "pending", sent: true }); setSentAt(Date.now());
    try {
      const reply = await send(session.sessionId);
      // The session moved while this was in flight: whatever it says now is newer than the reply.
      setOverride(latest.current === started ? { state: reply[field] ?? null, base: started } : undefined);
    }
    catch { setOverride(undefined); setSentAt(0); setError("Could not send your request. Try again."); } // not sent: Ask again stays offered
    finally { setSaving(false); }
  }, [send, field, session.sessionId]);
  return { state, saving, error, ask, supported: !!send, again, lapsed };
}

/** The line under a teammate's session they cannot type into (owner ruling 2026-10-26): members
 *  never ask to join; they type while the owner lets teammates control the session. */
export const watchOnlyLine = (ownerName: string) => `Only ${ownerName} can type in this session.`;

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
  session: "this conversation",
  similar: "similar commands",
};

/** The approval card's button for one offered choice. */
export function approvalModeAction(mode: LocalSessionApprovalMode, pending: Pick<LocalSessionPendingApproval, "similar">): string {
  if (mode === "session") return "Allow for this conversation";
  if (mode === "similar") return `Allow similar commands for this session (${pending.similar ?? ""})`;
  return `Approve and switch to ${APPROVAL_MODE_LABEL[mode]}`;
}

/** Whose permission prompt the card answers. */
export const approvalAgent = (provider: string): string => (provider === "codex" ? "Codex" : "Claude");

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
    : [session.model, session.cwdName ?? ""];
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
  end: "ended the session",
  approve: "approved a permission prompt",
  answer: "answered a question",
  deny: "denied a permission prompt",
  team_control_on: "let teammates control this session",
  team_control_off: "turned off teammate control",
  team_control_ended: "",
  share_link_created: "created a share link",
  share_link_revoked: "revoked a share link",
  share_link_audience: "changed who the link is for",
  session_copied: "took a copy to continue in their Claude Code",
  pickup_on: "let teammates pick up this session",
  pickup_off: "turned off pick-up and deleted its snapshot",
  picked_up: "picked this session up on their Mac",
  pickup_expired: "",
  pickup_requested: "asked to pick up this session",
  pickup_request_declined: "",
  join_requested: "asked to join this session",
  join_allowed: "",
  join_declined: "",
  join_allow_undone: "",
  ask_to_join_on: "let people watching the link ask to join",
  ask_to_join_off: "stopped people watching the link from asking to join",
  sender_removed: "",
  resume_requested: "asked to resume this session",
  resume_revoked: "",
};

/** Owner decisions about one person, by the person they were about. */
const possessive = (subject: string) => (subject === "you" ? "your" : `${subject}'s`);
const SUBJECT_LINE: Partial<Record<LocalSessionAuditEntry["action"], (subject: string) => string>> = {
  join_allowed: subject => `let ${subject} send to this session`,
  pickup_request_declined: subject => `declined ${possessive(subject)} request to pick up this session`,
  join_declined: subject => `declined ${possessive(subject)} request to join`,
  join_allow_undone: subject => `could not add ${subject} to the workspace, so ${possessive(subject)} request is open again`,
  sender_removed: subject => `removed ${subject} from this session`,
  resume_revoked: subject => `stopped ${subject} from resuming this session`,
};

/** "Aden approved a permission prompt from Lightbulb" -- the transcript's audit line. */
export function auditLine(entry: LocalSessionAuditEntry, nameOf: (userId: string) => string): string {
  if (entry.action === "team_control_ended") return "Teammate control turned off when the session ended";
  if (entry.action === "pickup_expired") return "Pick-up turned off and its snapshot was deleted, 7 days after the session ended";
  // The taker is often outside the workspace, so the entry's detail carries their name.
  if (entry.action === "session_copied") return `${entry.detail || (entry.actorUserId ? nameOf(entry.actorUserId) : "Someone")} ${ACTION_LINE.session_copied}`;
  const who = entry.actorUserId ? nameOf(entry.actorUserId) : entry.actorAgentId ? "An agent" : "Lightbulb";
  const failed = entry.state === "rejected" ? " (not delivered)" : entry.state === "unknown" ? " (delivery unknown)" : "";
  const subjectLine = SUBJECT_LINE[entry.action];
  // nameOf says "You" for the viewer; mid-sentence that is "you".
  const subject = entry.subjectUserId ? nameOf(entry.subjectUserId) : "a teammate";
  if (subjectLine) return `${who} ${subjectLine(subject === "You" ? "you" : subject)}`;
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

/** A provider-held question uses the command journal, never terminal key guesses.
 * An uncertain submission keeps its exact request and answers while checking delivery.
 */
export function useSessionQuestion(client: SessionControlClient, session: LocalSession, uuid: () => string) {
  const [state, setState] = useState<{ promptId: string; status: "sending" | "pending" | "applied" | "uncertain" | "rejected"; error?: string } | null>(null);
  const attempt = useRef<Extract<LocalSessionCommand, { kind: "answer" }> | null>(null);
  const busy = useRef(false);
  const pending = session.pendingQuestion;
  const current = state?.promptId === pending?.promptId ? state : null;
  const fail = useCallback((error: unknown, promptId: string) => {
    const rejected = error instanceof ApiError && [400, 403, 404, 409, 422].includes(error.status);
    setState({ promptId, status: rejected ? "rejected" : "uncertain",
      ...(rejected ? { error: error instanceof Error ? error.message : "This answer was refused." } : {}) });
  }, []);
  const sync = useCallback((receipt: LocalSessionCommandReceipt, promptId: string) => {
    setState({ promptId, status: receipt.state === "applied" ? "applied" : receipt.state === "rejected" ? "rejected" : receipt.state === "unknown" ? "uncertain" : "pending",
      ...(receipt.state === "rejected" ? { error: "This question is no longer accepting that answer." } : {}) });
  }, []);
  const answer = useCallback(async (answers: Record<string, string>) => {
    if (!pending || busy.current || (current && current.status !== "rejected")) return;
    const request: Extract<LocalSessionCommand, { kind: "answer" }> = {
      kind: "answer", requestId: uuid(), expectedGeneration: session.generation,
      promptId: pending.promptId, inputDigest: pending.inputDigest, answers: { ...answers },
    };
    attempt.current = request;
    busy.current = true;
    setState({ promptId: pending.promptId, status: "sending" });
    try { sync(await client.submitLocalSessionCommand(session.sessionId, request), pending.promptId); }
    catch (e) { fail(e, pending.promptId); } finally { busy.current = false; }
  }, [pending, current, client, session.sessionId, session.generation, uuid, sync, fail]);
  const promptId = pending?.promptId;
  const status = current?.status;
  useEffect(() => {
    if (!promptId || !status || !["pending", "uncertain"].includes(status) || !client.getLocalSessionCommand) return;
    const request = attempt.current;
    if (!request || request.promptId !== promptId || request.expectedGeneration !== session.generation) return;
    let live = true;
    let checking = false;
    let settled = false;
    const check = async () => {
      if (!live || checking || settled) return;
      checking = true;
      const accept = (receipt: LocalSessionCommandReceipt) => {
        if (!live) return;
        settled = receipt.state === "applied" || receipt.state === "rejected";
        sync(receipt, request.promptId);
      };
      const refuse = (error: unknown) => {
        if (!live) return;
        settled = error instanceof ApiError && [400, 403, 404, 409, 422].includes(error.status);
        fail(error, request.promptId);
      };
      try { accept(await client.getLocalSessionCommand!(session.sessionId, request.requestId)); }
      catch (error) {
        // If the initial HTTP request never arrived, reuse its immutable ID and answer.
        // Both journals deduplicate it, including a race with the first submission.
        if (live && error instanceof ApiError && error.status === 404) {
          try { accept(await client.submitLocalSessionCommand(session.sessionId, request)); }
          catch (error) { refuse(error); }
        } else { refuse(error); }
      } finally { checking = false; }
    };
    const timer = setInterval(() => { void check(); }, 2000);
    return () => { live = false; clearInterval(timer); };
  }, [client, session.sessionId, session.generation, promptId, status, sync, fail]);
  return { answer, state: current, locked: !!current && current.status !== "rejected" };
}

export type ApprovalAnswer = { decision: "allow" | "deny"; mode?: LocalSessionApprovalMode; reason?: string };

/**
 * Answer the prompt a session holds, once. The request id is minted per prompt
 * and answer, so a retry of the same click is idempotent and never a second answer.
 */
/**
 * `sha256Hex` hashes what a `full` prompt showed: an allow names that digest
 * (`shownDigest`) so the backend admits it only if the viewer saw the whole call.
 */
export function useSessionApproval(client: Pick<Client, "submitLocalSessionCommand"> & Partial<Pick<Client, "getLocalSession">>,
  session: LocalSession, uuid: () => string, sha256Hex: (text: string) => Promise<string>) {
  const [state, setState] = useState<{ approvalId: string; status: "sending" | "sent" | "failed" | "gone"; error?: string; base?: LocalSession } | null>(null);
  const ids = useRef(new Map<string, string>());
  const latest = useRef(session);
  latest.current = session;
  // A prompt known to be closed is hidden only on the session row that still showed it: any
  // later row that carries the same prompt is newer than that verdict, so it shows again.
  // Keyed by the prompt answered, too: a verdict on A never hides a prompt B that replaced it.
  const pending = state?.status === "gone" && state.base === session && state.approvalId === session.pendingApproval?.approvalId
    ? undefined : session.pendingApproval;
  const answer = useCallback(async ({ decision, mode, reason }: ApprovalAnswer) => {
    if (!pending) return;
    const text = decision === "deny" ? clipBytes(reason?.trim() ?? "", 500) : "";
    const key = `${pending.approvalId}:${decision}:${mode ?? ""}:${text}`;
    const requestId = ids.current.get(key) ?? uuid();
    ids.current.set(key, requestId);
    setState({ approvalId: pending.approvalId, status: "sending" });
    try {
      const shownDigest = decision === "allow" && pending.full && pending.input ? await sha256Hex(canonicalJson(pending.input)) : "";
      const receipt = await client.submitLocalSessionCommand(session.sessionId, {
        requestId, expectedGeneration: session.generation, kind: "approve", approvalId: pending.approvalId,
        inputDigest: pending.inputDigest, decision, ...(mode ? { mode } : {}),
        ...(text ? { text } : {}), ...(shownDigest ? { shownDigest } : {}),
      });
      // Admitted is not delivered: a prompt answered in the terminal first comes back rejected.
      setState(receipt.state === "rejected"
        ? { approvalId: pending.approvalId, status: "gone", base: latest.current }
        : { approvalId: pending.approvalId, status: "sent" });
    } catch (e) {
      const code = e instanceof ApiError ? (e.body as { code?: unknown } | null)?.code : undefined;
      if (e instanceof ApiError && e.status === 409 && code !== "LOCAL_SESSION_UNSUPPORTED") {
        // A conflict is also a busy row (the write's retries ran out): only the row itself says the prompt closed.
        const now = await client.getLocalSession?.(session.sessionId).catch(() => null);
        const open = !now || (now.status === "waiting-approval" && now.pendingApproval?.approvalId === pending.approvalId);
        setState(open
          ? { approvalId: pending.approvalId, status: "failed", error: "Could not send the answer. Try again." }
          : { approvalId: pending.approvalId, status: "gone", base: latest.current });
        return;
      }
      // LOCAL_SESSION_UNSUPPORTED is a 409 too, but the prompt is still open: say why, in the server's words.
      setState({ approvalId: pending.approvalId, status: "failed", error: e instanceof ApiError && e.status === 409 ? e.message
        : e instanceof ApiError && e.status === 403 ? "You can no longer control this session."
          : "Could not send the answer. Try again." });
    }
  }, [client, pending, session.sessionId, session.generation, uuid, sha256Hex]);
  const current = state && pending && state.approvalId === pending.approvalId ? state : null;
  return { pending, answer, state: current };
}

/**
 * Whether a `full` prompt's input hashes to the digest the Mac bound it to:
 * the gate for allowing a non-exact call remotely. False until the hash is
 * known and false on any mismatch or error (fails closed).
 */
export function useShownInputMatches(pending: LocalSessionPendingApproval | undefined, sha256Hex: (text: string) => Promise<string>): boolean {
  // A call with hidden characters is deny-only: what is drawn may not be what runs.
  const input = pending?.full && !hasHiddenChars(pending.input) ? pending.input : undefined;
  const expected = pending?.inputDigest;
  const key = input && expected ? `${pending?.approvalId}:${expected}` : null;
  // Bound to the exact input object hashed: a new input reads false until it is hashed too.
  const [matched, setMatched] = useState<{ key: string; input: Record<string, unknown> } | null>(null);
  useEffect(() => {
    if (!key || !input) return;
    let active = true;
    void sha256Hex(canonicalJson(input))
      .then(hex => { if (active) setMatched(hex === expected ? { key, input } : null); })
      .catch(() => { if (active) setMatched(null); });
    return () => { active = false; };
  }, [key, input, expected, sha256Hex]);
  return key !== null && matched?.key === key && matched.input === input;
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
  const [error, setError] = useState<string | null>(null);
  const [override, setOverride] = useState<boolean | null>(null);
  useEffect(() => { setOverride(null); }, [session.shareUsage]);
  const set = useCallback(async (next: boolean) => {
    if (!client.setLocalSessionUsageSharing) return;
    setSaving(true); setError(null);
    try { setOverride((await client.setLocalSessionUsageSharing(session.sessionId, next)).shareUsage === true); }
    catch { setError("Could not change who sees token usage."); } // the switch stays where the server left it
    finally { setSaving(false); }
  }, [client, session.sessionId]);
  return { enabled: override ?? session.shareUsage === true, saving, error, set };
}

const megabytes = (bytes: number) => (bytes >= 1_048_576 ? `${(bytes / 1_048_576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`);

/** "github.com/acme/app on fix/login", "acme/app", or "" when the snapshot holds no files. */
export function pickupSource(pickup: LocalSessionPickup | undefined): string {
  if (!pickup?.hasCode || !pickup.repo) return "";
  const repo = pickup.repo.split("/").slice(-2).join("/");
  return pickup.branch ? `${repo} on ${pickup.branch}` : repo;
}

/** What a live terminal says about its audience: "2 watching · 3 via link" while people watch by link; "" otherwise (also for an older answer with no counts). */
export function audienceLine(counts: unknown): string {
  const { watching = 0, viaLink = 0 } = (counts ?? {}) as { watching?: number; viaLink?: number };
  return viaLink ? [watching && `${watching} watching`, `${viaLink} via link`].filter(Boolean).join(" · ") : "";
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

/** How long a resume request waits for its Mac (the backend's 24 h), and how often a view asks. */
const RESUME_LIFETIME_MS = 24 * 60 * 60 * 1000;
const RESUME_FAST_MS = 30_000;
const RESUME_SLOW_MS = 30_000;
const OPENING_SLOW = "Your Mac has not opened this conversation yet. Check that Lightbulb is running and connected, then check again.";

const RESUME_REFUSED: Record<string, (mac: string, folder: string) => string> = {
  // Only a run Lightbulb cannot show (a private `claude --resume`, `codex --no-daemon`, a job it has
  // not picked up): a conversation it tracks in the provider's own daemon is shown again instead (resume.rs `reattach`).
  ALREADY_RUNNING: mac => `it is still running on ${mac}, where Lightbulb cannot show it. End it there, then resume`,
  FOLDER_NOT_APPROVED: (mac, folder) => `${folder} is no longer an approved folder on ${mac}`,
  FOLDER_MISSING: mac => `its folder is no longer on ${mac}`,
  TOOL_UNAVAILABLE: mac => `its CLI is no longer installed on ${mac}`,
  SHARING_OFF: mac => `session sharing is off on ${mac}`,
  // Claude continued it under a new id (←, /background): resuming this one would branch it.
  CONTINUED: mac => `it continues as a newer session on ${mac}. Open that session instead`,
};

/** One line about a resume already asked for (waiting, or refused by the Mac), or null. */
export function resumeStatus(session: LocalSession, own: boolean, ownerName: string, nameOf: (userId: string) => string, userId?: string | null): string | null {
  if (session.status !== "ended") return null;
  const resume = session.resume;
  if (!resume) return null;
  const mac = own ? "your Mac" : `${ownerName}’s Mac`;
  if (resume.state === "failed") return `Not resumed: ${(RESUME_REFUSED[resume.reason ?? ""] ?? ((m: string) => `${m} could not start it`))(mac, session.cwdName || "its folder")}.`;
  const who = resume.byUserId === userId ? "You" : nameOf(resume.byUserId);
  return who === "You" ? `Opening this session on ${mac}…` : `${who} asked to open this session on ${mac}.`;
}

export type SessionResumeFlow = {
  automatic?: boolean;
  available: boolean; status: string | null; saving: boolean; error: string | null;
  recovery?: "folder-access" | "sharing";
  check?: () => Promise<void>;
  resume: (message: string) => Promise<boolean>;
  /** Where the first message stands once the session runs again; null before, and once it landed. */
  chip: SessionReceiptChip | null;
};

/** The first message of each resume this page asked for, by session: the view that shows its
 * delivery may remount when the session starts running again. */
const firstMessages = new Map<string, { requestId: string; text: string }>();

export function useSessionResume(client: SessionControlClient, prop: LocalSession, opts: {
  own: boolean; userId?: string | null; ownerName: string; nameOf: (userId: string) => string; uuid: () => string;
  /** Opening the owner's desktop conversation restores it without sending a turn. Search hits omit this. */
  autoOpen?: boolean;
  /** Owned by the selected conversation, so generation remounts cannot open it twice. */
  openedConversation?: { current: string | null };
}): SessionResumeFlow & { session: LocalSession } {
  const { own, userId, ownerName, nameOf, uuid } = opts;
  const automatic = own && opts.autoOpen === true;
  const [autoOpening, setAutoOpening] = useState(automatic && prop.status === "ended");
  // Ended sessions are absent from workspace listings, so the page's copy goes stale once a resume
  // is asked or refused: keep the newer read while this is still the same ended run.
  const source = `${prop.sessionId}:${prop.generation}:${prop.status}`;
  const [fresh, setFresh] = useState<{ source: string; session: LocalSession } | null>(null);
  const session = fresh?.source === source ? fresh.session : prop;
  const [checked, setChecked] = useState<string | null>(null);
  const localOpened = useRef<string | null>(null);
  const opened = opts.openedConversation ?? localOpened;
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
      if (updated) setFresh({ source, session: updated });
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
  }, [client, session.sessionId, source, uuid]);
  // A stale sidebar row must not launch a second writer. Read the session before
  // restoring; a failed read leaves an explicit retry instead of guessing.
  useEffect(() => {
    if (!automatic) return;
    if (prop.status !== "ended") {
      opened.current = prop.sessionId;
      setAutoOpening(false); setError(null);
      return;
    }
    if (!client.getLocalSession) return;
    setAutoOpening(opened.current !== prop.sessionId);
    let alive = true;
    void client.getLocalSession(prop.sessionId).then(latest => {
      if (alive) {
        if (latest.status !== "ended" || latest.resume?.state === "pending") opened.current = prop.sessionId;
        if (latest.status !== "ended") { setAutoOpening(false); setError(null); }
        setFresh({ source, session: latest }); setChecked(source);
      }
    }, () => { if (alive) setError("Could not check this session. Try again."); });
    return () => { alive = false; };
  }, [automatic, client, prop.sessionId, prop.status, source, opened]);
  useEffect(() => {
    if (!automatic || checked !== source || opened.current === session.sessionId || session.resume?.state === "failed"
      || !canResumeSession(session, own, userId)) return;
    opened.current = session.sessionId;
    void resume("");
  }, [automatic, checked, source, session, own, userId, resume, opened]);
  const pending = session.status === "ended" && session.resume?.state === "pending";
  // shortcut: poll while the sidebar is stale; use status events if the five-second delay becomes visible.
  const following = automatic && prop.status === "ended" && session.status !== "ended";
  const requestedAt = session.resume?.requestedAt;
  const checkOpening = useCallback(async () => {
    if (!client.getLocalSession) return;
    try {
      const latest = await client.getLocalSession(session.sessionId);
      setFresh({ source, session: latest });
      if (latest.status === "ended" && latest.resume?.state === "pending") {
        setError(OPENING_SLOW);
      } else {
        setAutoOpening(false);
        setError(latest.status === "ended" && !latest.resume ? "This session did not open. Try again." : null);
      }
    } catch { setError("Could not check this session. Try again."); }
  }, [client, session.sessionId, source]);
  useEffect(() => {
    if (!automatic || !pending) return;
    const requested = Date.parse(requestedAt ?? "");
    const age = Number.isFinite(requested) ? Math.max(0, Date.now() - requested) : 0;
    const timer = setTimeout(() => setError(OPENING_SLOW), Math.max(0, 30_000 - age));
    return () => clearTimeout(timer);
  }, [automatic, pending, requestedAt]);
  useEffect(() => {
    const read = client.getLocalSession;
    if ((!pending && !following) || !read) return;
    // A request waits up to 24 h for its Mac, which can refuse whenever it wakes: keep asking
    // while it is live, every 2 s for the first 30 s and every 30 s after.
    const expires = Date.parse(requestedAt ?? "") + RESUME_LIFETIME_MS; // NaN: unknown, poll until unmount
    const started = Date.now();
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const next = () => {
      if (!alive) return;
      if (pending && Date.now() >= expires) { setError("The request to open this session expired. Check again to refresh its status."); return; }
      const pendingInterval = Date.now() - started < RESUME_FAST_MS ? AUTO_CHECK_INTERVAL_MS : RESUME_SLOW_MS;
      timer = setTimeout(() => void tick(), following ? 5000 : pendingInterval);
    };
    const tick = async () => {
      try {
        const latest = await read(session.sessionId);
        if (!alive) return;
        setFresh({ source, session: latest });
        if (following && latest.status === "ended") {
          setAutoOpening(false);
          setError("This session stopped before it was ready. Try again.");
          return;
        }
        if (latest.status === "ended" && !latest.resume) setError("This session did not open. Try again.");
        if (!following && (latest.status !== "ended" || latest.resume?.state !== "pending")) return; // resolved or refused
      } catch { /* the next tick asks again */ }
      next();
    };
    next();
    return () => { alive = false; clearTimeout(timer); };
  }, [client, session.sessionId, pending, following, requestedAt, source]);
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
    session, automatic: automatic && autoOpening,
    available: !!client.resumeLocalSession && canResumeSession(session, own, userId),
    status: resumeStatus(session, own, ownerName, nameOf, userId),
    recovery: own && session.resume?.state === "failed"
      ? session.resume.reason === "FOLDER_NOT_APPROVED" ? "folder-access" : session.resume.reason === "SHARING_OFF" ? "sharing" : undefined
      : undefined,
    saving, error, resume, check: client.getLocalSession ? checkOpening : undefined, chip: receiptChip(attempt),
  };
}
