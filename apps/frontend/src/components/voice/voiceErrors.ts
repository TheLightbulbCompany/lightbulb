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
