/**
 * The "Ask AI" section's prompt and provider deep links.
 *
 * Isolated from the component so the copy can be tuned in one place without
 * touching markup, and so the URL building is testable on its own.
 *
 * ⚠️ THESE PREFILL PARAMS ARE UNOFFICIAL. None of these vendors documents a
 * "open your composer with this text" API; `?q=` is a convention that has held
 * so far and can be dropped without notice. Two consequences the UI relies on:
 *
 *   1. Gemini has NO working prefill at all — `gemini.google.com/app?q=` is
 *      accepted and ignored, so that tile opens an empty Gemini on purpose.
 *   2. Because any of these can silently stop prefilling, AskAI also copies the
 *      prompt to the clipboard on every tile click. That is the fallback: the
 *      user lands on an empty composer and can paste. Do not remove the copy
 *      side-effect thinking it is redundant with the href — it is the net.
 *
 * Re-verify by hand (click each tile) before shipping a change here.
 */

/** What we want the AI to go and say about us. */
export const ASK_AI_PROMPT = [
  "Go to flickmeon.ai and read their site, then tell me if I've got this right:",
  "Lightbulb is a messaging workspace that ships with AI agents already",
  "integrated — no Slack bot to set up, no wiring tools together. My small team",
  "and my AI agent employees work in the same channels, and everything in the",
  "workspace — the task board, the connected apps — is queryable by the agents.",
  "How does that compare to running Slack plus separate AI tools, in terms of",
  "setup, simplicity, and pricing?",
].join(" ");

export type AskAiProvider = {
  /** Stable key, used for the React list and as a test handle. */
  id: string;
  /** Tile label, e.g. "Ask ChatGPT". */
  label: string;
  /** Base URL; the prompt is appended as the `q` param. */
  base: string;
};

/**
 * Order matters — it is the visual order of the 3x2 grid, and "Copy prompt"
 * (rendered by the component, not listed here) takes the sixth cell.
 */
export const ASK_AI_PROVIDERS: AskAiProvider[] = [
  { id: "chatgpt", label: "Ask ChatGPT", base: "https://chatgpt.com/" },
  { id: "claude", label: "Ask Claude", base: "https://claude.ai/new" },
  { id: "gemini", label: "Ask Gemini", base: "https://gemini.google.com/app" },
  { id: "perplexity", label: "Ask Perplexity", base: "https://www.perplexity.ai/search" },
  { id: "grok", label: "Ask Grok", base: "https://grok.com/" },
];

/** Build a provider's deep link with the prompt URL-encoded into `?q=`. */
export function askAiUrl(provider: AskAiProvider, prompt: string = ASK_AI_PROMPT): string {
  const url = new URL(provider.base);
  url.searchParams.set("q", prompt);
  return url.toString();
}
