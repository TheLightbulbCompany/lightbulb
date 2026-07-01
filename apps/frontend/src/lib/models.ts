/**
 * A single model from OpenClaw's per-agent `models.list`. Unlike the legacy
 * ModelSelector which derived the provider by splitting the id on ".", the
 * catalog carries `provider` and `id` as SEPARATE fields — the composer passes
 * both to sessions.patch and rebuilds `${provider}/${id}` (Bedrock ids are
 * application-inference-profile ARNs full of dots that a split corrupts).
 */
export interface ModelEntry {
  id: string;
  provider: string;
  name?: string;
  /** Owner has access to this model (drives the disabled + "access denied" hint). */
  available?: boolean;
  /** Explicit reasoning capability if the catalog reports it. */
  reasoning?: boolean;
}

export function buildModelRef(provider: string, id: string): string {
  return `${provider}/${id}`;
}

/**
 * Best-effort `sessions.list` row → model ref (`${provider}/${id}`) for
 * hydrating the composer pill. The row may report `model` already as a full
 * ref (what `sessions.patch` wrote), or as a bare id alongside a separate
 * `modelProvider`. Only join when the model isn't already provider-prefixed —
 * Bedrock ids are ARNs with internal slashes, so a `startsWith(provider + "/")`
 * check (NOT a bare "/" test) is what disambiguates. Returns "" when no model
 * is persisted, so the pill falls back to the agent default.
 */
export function resolveSessionModelRef(model?: string, modelProvider?: string): string {
  if (!model) return "";
  if (modelProvider && !model.startsWith(`${modelProvider}/`)) {
    return buildModelRef(modelProvider, model);
  }
  return model;
}

// Reasoning families we know support a thinking/effort budget. Kept strict to
// current usage (no migration-history widening): OpenAI o-series + gpt-5, and
// Anthropic Claude 3.7 / 4 (extended thinking). Non-matches hide the effort UI.
const REASONING_ID_RE =
  /(^|[/.:_-])(o1|o3|o4|gpt-5|claude-3-7|claude-sonnet-4|claude-opus-4|sonnet-4|opus-4)/i;

export function isReasoningModel(entry: ModelEntry): boolean {
  if (typeof entry.reasoning === "boolean") return entry.reasoning;
  return REASONING_ID_RE.test(entry.id);
}

export function providerLabelFor(entry: ModelEntry): string {
  return entry.provider || "unknown";
}
