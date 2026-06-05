// Pure mapping between Isol8's billing-side provider_choice and the
// container's live models.authStatus snapshot. Extracted from LLMPanel so the
// provider-id resolution + status→color rules are testable without React.

export type ProviderChoice = "chatgpt_oauth" | "byo_key" | "bedrock_claude";
export type ByoProvider = "openai" | "anthropic";

// Upstream enum: src/agents/auth-health.ts — "ok"|"expiring"|"expired"|"missing"|"static".
export type AuthProviderHealthStatus =
  | "ok"
  | "expiring"
  | "expired"
  | "missing"
  | "static";

export interface AuthStatusProvider {
  provider: string;
  displayName: string;
  status: AuthProviderHealthStatus;
}

export type DotColor = "green" | "amber" | "red" | "neutral";

// Healthy = green (ok, and static creds which never expire); amber =
// expiring soon; red = unusable (expired/missing). Anything we don't
// recognize is neutral rather than a false-green.
export function statusToDotColor(status: string | undefined): DotColor {
  if (status === "ok" || status === "static") return "green";
  if (status === "expiring") return "amber";
  if (status === "expired" || status === "missing") return "red";
  return "neutral";
}

function normalizeId(id: string): string {
  return id.trim().toLowerCase();
}

// Candidate container provider ids for a given Isol8 provider choice. Match is
// exact-or-prefix on the normalized id because the container may report a more
// specific variant (e.g. openai-codex, openai-responses) than our coarse
// choice. ChatGPT OAuth is the openai-codex provider upstream.
function candidateIds(
  choice: ProviderChoice,
  byoProvider: ByoProvider | null,
): string[] {
  // The container writes ``models.providers.amazon-bedrock`` and reports that
  // id back through models.authStatus (upstream normalizeProviderId only
  // lowercases — it does NOT map ``amazon-bedrock`` → ``bedrock``), so the
  // real reported id is ``amazon-bedrock``; ``bedrock`` is a tolerant fallback.
  if (choice === "bedrock_claude") return ["amazon-bedrock", "bedrock"];
  if (choice === "chatgpt_oauth") return ["openai-codex"];
  // byo_key
  if (byoProvider === "anthropic") return ["anthropic"];
  if (byoProvider === "openai") return ["openai"];
  return [];
}

export function resolveProviderStatus(
  choice: ProviderChoice | null | undefined,
  byoProvider: ByoProvider | null | undefined,
  list: AuthStatusProvider[] | undefined,
): AuthProviderHealthStatus | undefined {
  if (!choice || !list || list.length === 0) return undefined;
  const wanted = candidateIds(choice, byoProvider ?? null);
  if (wanted.length === 0) return undefined;
  const match = list.find((p) => {
    const id = normalizeId(p.provider);
    return wanted.some((w) => id === w || id.startsWith(`${w}-`));
  });
  return match?.status;
}
