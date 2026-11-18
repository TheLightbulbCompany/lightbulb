/** Values are the backend's `schemas/setup_request.py` enums; keep in step. */
export const TEAM_SIZES = [
  { value: "just_me", label: "Just me" },
  { value: "2", label: "2" },
  { value: "3-5", label: "3–5" },
  { value: "6-15", label: "6–15" },
  { value: "16+", label: "16 or more" },
] as const;

/**
 * "Where do your bookings live?" The schema has no field for it, so the form
 * sends `agents: ["other"]` (required, one of the schema's values) and puts this
 * answer in `agents_other`. The labels are only a question, never a claim that
 * Lightbulb books into these apps.
 */
export const BOOKING_TOOLS = [
  "Google Calendar",
  "Square",
  "Vagaro",
  "Fresha",
  "Booksy",
  "GlossGenius",
  "Boulevard",
  "A paper book or the phone",
  "Something else",
] as const;

/** A subset of the schema's sources, in the words a shop owner uses. */
export const SOURCES = [
  { value: "friend", label: "A friend or another owner" },
  { value: "search", label: "Google or another search engine" },
  { value: "ai_assistant", label: "Asked an AI (ChatGPT, Claude, Perplexity)" },
  { value: "founder_email", label: "An email from one of us" },
  { value: "reddit", label: "Reddit" },
  { value: "linkedin", label: "LinkedIn" },
  { value: "other", label: "Somewhere else (tell us)" },
] as const;
