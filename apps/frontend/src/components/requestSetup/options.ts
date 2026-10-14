/** Values are the backend's `schemas/setup_request.py` enums; keep in step. */
export const TEAM_SIZES = [
  { value: "just_me", label: "Just me" },
  { value: "2", label: "2" },
  { value: "3-5", label: "3–5" },
  { value: "6-15", label: "6–15" },
  { value: "16+", label: "16+" },
] as const;

export const AGENTS = [
  { value: "claude_code", label: "Claude Code" },
  { value: "codex", label: "Codex" },
  { value: "chatgpt", label: "ChatGPT" },
  { value: "cursor", label: "Cursor" },
  { value: "other", label: "Other" },
] as const;

export const SOURCES = [
  { value: "hacker_news", label: "Hacker News" },
  { value: "reddit", label: "Reddit" },
  { value: "x", label: "X" },
  { value: "linkedin", label: "LinkedIn" },
  { value: "product_hunt", label: "Product Hunt" },
  { value: "shared_session", label: "A shared Claude Code or Codex session" },
  { value: "plugin", label: "The Claude Code or Codex plugin marketplace" },
  { value: "friend", label: "A friend or colleague" },
  { value: "founder_email", label: "An email from a founder" },
  { value: "search", label: "Search engine" },
  { value: "ai_assistant", label: "Asked an AI (ChatGPT, Claude, Perplexity)" },
  { value: "other", label: "Other (tell us)" },
] as const;

/** Team sizes the guided setups are not for: they get the start-free confirmation. */
export const SMALL_TEAMS = new Set<string>(["just_me", "2"]);
