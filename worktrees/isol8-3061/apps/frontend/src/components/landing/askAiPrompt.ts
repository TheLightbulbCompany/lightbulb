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

/**
 * What we want the AI to go and say about us.
 *
 * It states the model up front rather than relying on the crawl alone: not
 * every provider fetches the page, and one that doesn't should still have
 * enough to answer from. The pitch is AI-native and multiplayer — agents as
 * members of the company, their work visible to the whole team — NOT a
 * feature-by-feature comparison against Slack. Owner direction, 2026-09-11.
 */
export const ASK_AI_PROMPT = [
  "Go to flickmeon.ai and read their site.",
  "Lightbulb is an AI-native team workspace — a chat app where AI agents are",
  "actual members of the company, not a bot someone bolted on. You hire agents,",
  "put them in channels, and @-mention them like colleagues: they read the",
  "thread above them, do the work, and report back where the whole team can see",
  "it. It's multiplayer by default, so an agent's context and output are shared",
  "with everyone instead of trapped in one person's private AI chat, and the",
  "whole workspace — the task board, the connected apps, the history — is",
  "queryable by the agents with no integration work.",
  "What does a team actually get out of working this way, and who is it a good",
  "fit for?",
].join(" ");

export type AskAiProviderId = "chatgpt" | "claude" | "gemini" | "perplexity" | "grok";

export type AskAiProvider = {
  /** Stable key, used for the React list and as a test handle. */
  id: AskAiProviderId;
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
