/** The Live worker's participant identity; only it may send `huddle.error` or answer the Talk RPCs. */
export const LIVE_AGENT_IDENTITY = "agent-ceo";

/** The channel header's marker for who is Live. Clients draw the live dot beside it (the `--live` colour). */
export function liveMarker(names: readonly string[]): string {
  const who = names.length <= 2 ? names.join(" and ") : `${names[0]} and ${names.length - 1} others`;
  return `Live — ${who}`;
}

/** Maps a failed join (an API error, or the mic refused / missing) to a plain error code. */
export function liveJoinErrorCode(e: unknown): string {
  const { status, body, name } = (e ?? {}) as { status?: number; name?: string; body?: { code?: string; detail?: unknown } | null };
  const raw = body?.code ?? (typeof body?.detail === "string" ? body.detail : (body?.detail as { code?: string } | undefined)?.code);
  const code = typeof raw === "string" ? raw.toLowerCase() : undefined; // an ApiProblem says ROOM_NOT_FOUND
  return name === "NotAllowedError" ? "mic_denied" : name === "NotFoundError" ? "no_mic"
    : status === 503 || code === "huddles_unavailable" ? "voice_unavailable"
    : code === "room_archived" || code === "room_not_found" || code === "huddles_upstream" || code === "workspace_not_ready" ? code
    : "connection_lost";
}

// ---- Per-agent Live (contract C4/C5): the worker's attributes and notices, read by the web app.

type Attrs = Readonly<Record<string, string>> | undefined;
/** Own-key lookup, so an attribute named `__proto__` or `constructor` reads as absent. */
const attr = (a: Attrs, k: string) => (a && Object.prototype.hasOwnProperty.call(a, k) ? a[k] : undefined);

export const LIVE_NOTICE_TOPIC = "live.notice";
export const SPEAKING_AGENT = "lightbulb.speaking_agent";
export const VOICE_LIMIT = "lightbulb.voice_limit";
export const statusKey = (id: string) => `lightbulb.agent.${id}.status`;
export const workingKey = (id: string) => `lightbulb.agent.${id}.working`;
export const handKey = (id: string) => `lightbulb.agent.${id}.hand`;

const STATUSES = ["joining", "speaking", "listening", "working", "has_news", "idle"] as const;
export type AgentLiveStatus = (typeof STATUSES)[number];

/** C4: status alone decides the bars and the state line; anything unknown is idle. */
export function agentLiveStatus(attrs: Attrs, id: string): AgentLiveStatus {
  const s = attr(attrs, statusKey(id));
  return (STATUSES as readonly string[]).includes(s ?? "") ? (s as AgentLiveStatus) : "idle";
}

/** The agent holding the floor (C4 `speaking_agent`), or null. */
export const floorHolder = (attrs: Attrs): string | null => attr(attrs, SPEAKING_AGENT) || null;
export const agentWorking = (attrs: Attrs, id: string) => (attr(attrs, workingKey(id)) ?? "").trim().slice(0, 60);
/** What a raised hand is about (set with `has_news`); "" while the worker does not publish it. */
export const agentHand = (attrs: Attrs, id: string) => (attr(attrs, handKey(id)) ?? "").trim().slice(0, 60);

export type AgentLineExtras = { working?: string; listeningTo?: string; queuePos?: number | null; hand?: string; meeting?: boolean };

/** The tile's state line. `meeting` words it for a Meet tile, where nobody can tap. */
export function agentStateLine(status: AgentLiveStatus, x: AgentLineExtras = {}): string {
  switch (status) {
    case "speaking": return "Speaking";
    case "joining": return "Joining…";
    case "listening": return x.listeningTo ? `Listening to ${x.listeningTo}` : "Listening";
    case "working":
      // a meeting agent that heard its name, before the request is understood ("listening to Priya")
      if (x.working?.startsWith("listening to ")) return `L${x.working.slice(1)}`;
      return x.working ? `Working: ${x.working}` : "Working";
    case "has_news":
      if (x.meeting) return `Hand up: ${x.hand || "has something for you"}`;
      // the topic is the task as it was asked ("what PRs are open"): "Has what PRs…" would not read
      return x.hand ? `Ready: ${x.hand} — tap to hear` : "Has something — tap to hear";
    default: return x.queuePos != null ? "Next" : "";
  }
}

const lowerFirst = (s: string) => s.charAt(0).toLowerCase() + s.slice(1);

/** One line naming an agent and its state ("Gauge — speaking", "Gauge is joining…"). */
export function agentLine(name: string, status: AgentLiveStatus, x: AgentLineExtras = {}): string {
  if (status === "joining") return `${name} is joining…`;
  if (status === "has_news" && !x.meeting) return x.hand ? `${name} is ready: ${x.hand} — tap to hear it` : `${name} has something — tap to hear it`;
  const line = agentStateLine(status, x);
  return line ? `${name} — ${lowerFirst(line)}` : name;
}

/** C5: what the worker sends people on `live.notice`. */
export type LiveNotice =
  | { kind: "renamed"; agentId: string; from: string; to: string; by: string; collides: boolean }
  | { kind: "voice_note_saved"; agentId: string; name: string; text: string; by: string }
  | { kind: "ambiguous_name"; agentId: string }
  | { kind: "voice_failed"; agentId: string };

/** Parses a `live.notice` body; anything malformed is null. Names cap at 80 chars, note text at 1,000. */
export function parseLiveNotice(raw: string): LiveNotice | null {
  let o: Record<string, unknown>;
  try { o = JSON.parse(raw); } catch { return null; }
  if (!o || typeof o !== "object") return null;
  const str = (k: string, cap: number) => (typeof o[k] === "string" ? (o[k] as string).slice(0, cap) : null);
  const agentId = str("agentId", 200);
  if (!agentId || agentId === "__proto__") return null;
  switch (o.kind) {
    case "renamed": {
      const [from, to, by] = [str("from", 80), str("to", 80), str("by", 80)];
      if (from === null || !to || by === null || typeof o.collides !== "boolean") return null;
      return { kind: "renamed", agentId, from, to, by, collides: o.collides };
    }
    case "voice_note_saved": {
      const [name, text, by] = [str("name", 80), str("text", 1000), str("by", 80)];
      if (!name || text === null || by === null) return null;
      return { kind: "voice_note_saved", agentId, name, text, by };
    }
    case "ambiguous_name":
    case "voice_failed":
      return { kind: o.kind, agentId };
    default:
      return null;
  }
}

export const renamedText = (from: string, to: string) => `${from} is now ${to}`;
export const voiceNoteText = (name: string, text: string) => `${name} will remember: ${text}`;

/** Why the backend refused to open agent voice (the worker's `lightbulb.voice_limit`), or null when voice is fine. */
export const voiceLimit = (a: Attrs): string | null => attr(a, VOICE_LIMIT)?.trim() || null;

/** The Meet tile's line for a refusal: nobody there can tap, and may not be the workspace's admin, so no "add credits". */
export const meetVoiceLimitText = (reason: string): string =>
  reason === "free_minutes_used" ? "Today's free voice minutes are used up. The agents can speak again tomorrow."
    : reason === "credits_low" ? "Voice is paused: the workspace's credits are low."
    : reason === "plan_inactive" ? "Voice is off: the workspace's plan is not active."
    : "The agents cannot speak right now.";

/** "+14842079294" as "(484) 207-9294"; anything else as it is. */
export const usPhone = (e164: string) => {
  const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(e164);
  return m ? `(${m[1]}) ${m[2]}-${m[3]}` : e164;
};
