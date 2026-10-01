const ERRORS = new Map<string, string>(Object.entries({
  free_minutes_used: "You've used today's free call minutes.",
  credits_low: "Add credits to keep calling.",
  plan_inactive: "Calls need an active plan.",
  voice_unavailable: "Voice calls aren't switched on yet.",
  connection_lost: "The call dropped.",
  voice_busy: "Someone in your workspace is already on a free call.",
  room_archived: "This channel is archived.",
  room_not_found: "This channel no longer exists.",
  huddles_upstream: "Live is having trouble right now. Try again.",
  mic_denied: "Allow microphone access to join.",
  no_agents: "This channel has no agents to talk to.",
  workspace_not_ready: "Your workspace is still getting ready. Try again in a minute.",
  lightbulb_unreachable: "Couldn't reach Lightbulb — try again.",
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

/** The Lightbulb tile's status, from the worker's participant attributes (huddle_agent.huddle `_state`). */
export function liveStatus(attrs: Readonly<Record<string, string>> | undefined): string {
  const speaker = attrs?.["lightbulb.speaker"];
  const working = attrs?.["lightbulb.working"] ?? "";
  switch (attrs?.["lightbulb.status"]) {
    case "listening": return speaker ? `Listening to ${speaker}` : "Listening";
    case "thinking": return "Thinking…";
    case "speaking": return "Speaking";
    case "working": {
      const [who, what] = working.split(" — ");
      return what ? `${who} is working: ${what}` : who ? `${who} is working` : "Thinking…";
    }
    default: return "Following along";
  }
}

/** The channel header's marker for who is Live. */
export function liveMarker(names: readonly string[]): string {
  const who = names.length <= 2 ? names.join(" and ") : `${names[0]} and ${names.length - 1} others`;
  return `🔴 Live — ${who}`;
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
