/**
 * Shared `POST /billing/provider` commit + error normalization.
 *
 * Two surfaces commit a provider choice for an already-subscribed owner:
 *   - `ChangeProviderModal` — the deliberate switch flow (every offered path).
 *   - `LLMPanel`'s empty state — the inline picker, which commits managed
 *     inference directly (no modal) because that path needs no credential
 *     collection.
 *
 * Both must surface the same backend errors, so the call and the message
 * extraction live here rather than being duplicated per surface.
 */

import { ApiError } from "@/lib/api";
import type { ProviderChoiceId } from "@/lib/providerCards";

export type ByoProvider = "openai" | "anthropic";

type PostFn = (endpoint: string, body: unknown) => Promise<unknown>;

export async function commitProviderChoice(
  post: PostFn,
  choice: ProviderChoiceId,
  byo?: ByoProvider,
): Promise<void> {
  await post("/billing/provider", {
    provider_choice: choice,
    ...(byo ? { byo_provider: byo } : {}),
  });
}

/**
 * Backend `ChangeProviderError` surfaces as `{code, message}` under FastAPI's
 * `detail` wrapper. Render the human message; the code stays in the network
 * response for ops debugging.
 */
export function providerCommitErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const data = e.body as { detail?: { message?: string } } | undefined;
    if (data?.detail?.message) return data.detail.message;
    if (e.message) return e.message;
  } else if (e instanceof Error) {
    return e.message;
  }
  return "Couldn't change provider.";
}

export function readableProvider(choice: ProviderChoiceId, byo?: ByoProvider): string {
  if (choice === "openrouter_managed") return "Lightbulb-managed models";
  if (choice === "chatgpt_oauth") return "ChatGPT OAuth";
  if (choice === "byo_key" && byo) return `${byo === "openai" ? "OpenAI" : "Anthropic"} (BYO key)`;
  return "BYO key";
}
