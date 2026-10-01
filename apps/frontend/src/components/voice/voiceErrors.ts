const ERRORS = new Map<string, string>(Object.entries({
  free_minutes_used: "You've used today's free call minutes.",
  credits_low: "Add credits to keep calling.",
  plan_inactive: "Calls need an active plan.",
  voice_unavailable: "Voice calls aren't switched on yet.",
  connection_lost: "The call dropped.",
  voice_busy: "Someone in your workspace is already on a free call.",
  room_archived: "This channel is archived.",
  room_not_found: "This channel no longer exists.",
  huddles_upstream: "Huddles are having trouble right now. Try again.",
  mic_denied: "Allow microphone access to join.",
  no_agents: "This channel has no agents to talk to.",
}));

/** Plain-word message for a code; unknown or non-string codes (incl. "__proto__") get the fallback. */
export const errorText = (code: unknown, fallback: string) => (typeof code === "string" ? ERRORS.get(code) : undefined) ?? fallback;
