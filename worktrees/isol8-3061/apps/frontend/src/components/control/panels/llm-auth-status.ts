// Pure mapping between Isol8's billing-side provider_choice and the
// container's live models.authStatus snapshot. Extracted from LLMPanel so the
// provider-id resolution + status→color rules are testable without React.
//
// `choice` is the STORED provider_choice string, not the picker's
// ProviderChoiceId — it can carry a legacy value (e.g. a retired
// `bedrock_claude` row) that is no longer offered by the picker, so this
// stays a plain string rather than importing the narrower picker union.
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
  choice: string,
  byoProvider: ByoProvider | null,
): string[] {
  if (choice === "openrouter_managed") return ["custom"];
  if (choice === "chatgpt_oauth") return ["openai-codex"];
  // byo_key
  if (byoProvider === "anthropic") return ["anthropic"];
  if (byoProvider === "openai") return ["openai"];
  return [];
}

// Exact ids reserved by EVERY provider choice. Used to keep one choice's prefix
// match from swallowing another choice's exact id: the BYO ``openai`` candidate
// must not prefix-match ``openai-codex`` (chatgpt_oauth's distinct provider),
// and ``anthropic``/``openai`` must not be claimed by an unrelated prefix
// either. Built from the full candidate union so adding a future provider
// choice keeps the exclusion correct automatically.
function allReservedExactIds(): Set<string> {
  return new Set([
    ...candidateIds("openrouter_managed", null),
    ...candidateIds("chatgpt_oauth", null),
    ...candidateIds("byo_key", "anthropic"),
    ...candidateIds("byo_key", "openai"),
  ]);
}

export function resolveProviderStatus(
  choice: string | null | undefined,
  byoProvider: ByoProvider | null | undefined,
  list: AuthStatusProvider[] | undefined,
): AuthProviderHealthStatus | undefined {
  if (!choice || !list || list.length === 0) return undefined;
  const wanted = candidateIds(choice, byoProvider ?? null);
  if (wanted.length === 0) return undefined;

  // Pass 1 — EXACT match in candidate-priority order. An exact hit on our own
  // id always beats a prefix hit that could collide with another choice's
  // reserved id.
  for (const w of wanted) {
    const match = list.find((p) => normalizeId(p.provider) === w);
    if (match) return match.status;
  }

  // Pass 2 — PREFIX match in candidate-priority order, but never claim an id
  // that is the EXACT reserved id of a DIFFERENT provider choice. This is what
  // stops the BYO ``openai`` candidate from matching ``openai-codex`` (the
  // chatgpt_oauth id) while still matching genuine variants like
  // ``openai-responses``. A candidate is allowed to prefix-match its own exact
  // id (already handled by pass 1), so excluding the full reserved set here is
  // safe.
  const reserved = allReservedExactIds();
  for (const w of wanted) {
    const match = list.find((p) => {
      const id = normalizeId(p.provider);
      return id.startsWith(`${w}-`) && !reserved.has(id);
    });
    if (match) return match.status;
  }
  return undefined;
}
