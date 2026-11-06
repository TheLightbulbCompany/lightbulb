const ERRORS = new Map<string, string>(Object.entries({
  free_minutes_used: "You've used today's free voice minutes. People can keep talking.",
  credits_low: "Add credits to keep talking to agents.",
  plan_inactive: "Calls need an active plan.",
  voice_unavailable: "Voice calls aren't switched on yet.",
  connection_lost: "The call dropped.",
  voice_busy: "Someone in your workspace is already on a free call.",
  room_archived: "This project is archived.",
  room_not_found: "This project no longer exists.",
  huddles_upstream: "Live is having trouble right now. Try again.",
  mic_denied: "Allow microphone access to join.",
  no_agents: "There are no agents here to talk to.",
  workspace_not_ready: "Your workspace is still getting ready. Try again in a minute.",
  lightbulb_unreachable: "Couldn't reach the agent — try again.",
  agents_absent: "Agents couldn't join this Live.",
  no_mic: "No microphone found.",
  idle_ended: "Live ended — no one was talking.",
}));

/** Plain-word message for a code; unknown or non-string codes (incl. "__proto__") get the fallback. */
export const errorText = (code: unknown, fallback: string) => (typeof code === "string" ? ERRORS.get(code) : undefined) ?? fallback;

/** The Live worker's participant identity; only it may send `huddle.error` or answer the Talk RPCs. */
export const LIVE_AGENT_IDENTITY = "agent-ceo";

/** A Live error: the worker's `huddle.error` code (with `reason` for voice_limit) or one of the client's own. */
export const liveErrorText = (e: { code: string; reason?: string }) =>
  errorText(e.code === "voice_limit" ? e.reason : e.code, "Live couldn't start.");

/** The channel header's marker for who is Live. Clients draw the live dot beside it (the `--live` colour). */
export function liveMarker(names: readonly string[]): string {
  const who = names.length <= 2 ? names.join(" and ") : `${names[0]} and ${names.length - 1} others`;
  return `Live — ${who}`;
}

/** Maps a failed join (an API error, or the mic refused / missing) to a code `errorText` knows. */
export function liveJoinErrorCode(e: unknown): string {
  const { status, body, name } = (e ?? {}) as { status?: number; name?: string; body?: { code?: string; detail?: unknown } | null };
  const raw = body?.code ?? (typeof body?.detail === "string" ? body.detail : (body?.detail as { code?: string } | undefined)?.code);
  const code = typeof raw === "string" ? raw.toLowerCase() : undefined; // an ApiProblem says ROOM_NOT_FOUND
  return name === "NotAllowedError" ? "mic_denied" : name === "NotFoundError" ? "no_mic"
    : status === 503 || code === "huddles_unavailable" ? "voice_unavailable"
    : code === "room_archived" || code === "room_not_found" || code === "huddles_upstream" || code === "workspace_not_ready" ? code
    : "connection_lost";
}

// ---- Per-agent Live (contract C4/C5): the worker's attributes and notices, read the same on web and iPhone.

type Attrs = Readonly<Record<string, string>> | undefined;
/** Own-key lookup, so an attribute named `__proto__` or `constructor` reads as absent. */
const attr = (a: Attrs, k: string) => (a && Object.prototype.hasOwnProperty.call(a, k) ? a[k] : undefined);

export const LIVE_NOTICE_TOPIC = "live.notice";
export const SPEAKING_AGENT = "lightbulb.speaking_agent";
export const QUEUE = "lightbulb.queue";
export const LISTENING_TO = "lightbulb.listening_to";
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

/** The pending multi-name request's agents in answer order, the floor-holder first. */
export const liveQueue = (attrs: Attrs): string[] =>
  [...new Set((attr(attrs, QUEUE) ?? "").split(",").map((s) => s.trim()).filter(Boolean))];

/** The queue badge: 1-based, never shown on the floor-holder (even while it is joining). */
export function queuePosition(attrs: Attrs, id: string): number | null {
  const i = liveQueue(attrs).indexOf(id);
  return i < 0 || id === floorHolder(attrs) ? null : i + 1;
}

/** Every room agent gets a Live tile, roster order, no humans: listen modes are chat-only (owner ruling 2026-10-02). */
export function liveAgents<M extends { kind: string }>(members: readonly M[]): M[] {
  return members.filter((m) => m.kind === "agent");
}

/** Stable order: speaking (the floor-holder listening counts) > joining > queued > has_news > working > idle. */
export function orderAgentSlots(ids: readonly string[], attrs: Attrs): string[] {
  const rank = (id: string) => {
    const s = agentLiveStatus(attrs, id);
    if (s === "speaking" || s === "listening") return 0;
    if (s === "joining") return 1;
    if (liveQueue(attrs).includes(id)) return 2;
    return s === "has_news" ? 3 : s === "working" ? 4 : 5;
  };
  return [...ids].sort((a, b) => rank(a) - rank(b));
}

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

/** Screen-reader name for an agent tile ("Forge, agent, next, working: drafting the launch post"). */
export function agentA11yName(name: string, status: AgentLiveStatus, x: AgentLineExtras = {}): string {
  const line = agentStateLine(status, { ...x, queuePos: null });
  return [name, "agent", x.queuePos != null && "next", line && lowerFirst(line)].filter(Boolean).join(", ");
}

const STATUS_KEY = /^lightbulb\.agent\.(.+)\.status$/;

/** The popover's "what's happening" line ("Gauge is speaking · Next: Forge · 2 working"), or "". */
export function liveHappening(attrs: Attrs, nameOf: (id: string) => string): string {
  const parts: string[] = [];
  const holder = floorHolder(attrs);
  if (holder) {
    const s = agentLiveStatus(attrs, holder);
    parts.push(`${nameOf(holder)} ${s === "joining" ? "is joining…" : s === "listening" ? "is listening" : "is speaking"}`);
  }
  const next = liveQueue(attrs)[1];
  if (next) parts.push(`Next: ${nameOf(next)}`);
  const working = Object.keys(attrs ?? {}).filter((k) => STATUS_KEY.test(k) && attr(attrs, k) === "working").length;
  if (working) parts.push(`${working} working`);
  return parts.join(" · ");
}

/** An announcement when the floor changes or an agent leaves; null when only working text or bars moved. */
export function floorChange(
  prevAttrs: Attrs, attrs: Attrs, prevIds: readonly string[], ids: readonly string[], nameOf: (id: string) => string,
): string | null {
  const left = prevIds.find((id) => !ids.includes(id));
  if (left) return `${nameOf(left)} left`;
  const h = floorHolder(attrs);
  if (!h) return null;
  const s = agentLiveStatus(attrs, h);
  const wasJoining = agentLiveStatus(prevAttrs, h) === "joining";
  if (h === floorHolder(prevAttrs) && (s === "joining") === wasJoining) return null;
  return `${nameOf(h)} is ${s === "joining" || s === "listening" ? s : "speaking"}`;
}

/** Does an agent's name sound like a person in the room (full name or first word, any case)? */
export function nameCollides(agentName: string, humans: readonly { displayName: string }[]): boolean {
  const n = agentName.trim().toLowerCase();
  return !!n && humans.some((h) => {
    const d = h.displayName.trim().toLowerCase();
    return d === n || d.split(/\s+/)[0] === n;
  });
}

/** The first-Live hint; a name that collides with a person is said "hey Nova". */
export function liveHint(agents: readonly { name: string; collides: boolean }[]): string | null {
  const say = (a: { name: string; collides: boolean }) => (a.collides ? `hey ${a.name}` : a.name);
  if (!agents.length) return null;
  if (agents.length === 1) return `Say "${say(agents[0])}, …" or tap ${agents[0].name} to talk.`;
  return `Say an agent's name — "${say(agents[0])}, …" — or tap them. "Lightbulb, …" asks the team.`;
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

/** Shown from the join until the voice worker is in the room: nobody hears an agent request yet. */
export const AGENTS_JOINING = "Agents are joining…";
export const wakingText = (name: string) => `${name} is waking up — this can take a minute the first time.`;
export const voiceFailedText = (name: string) => `${name} couldn't join. Try again.`;
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
