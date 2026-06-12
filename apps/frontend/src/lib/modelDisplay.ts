/**
 * Human-readable model names for agent surfaces.
 *
 * Bedrock model ids are per-owner application-inference-profile ARNs
 * (`amazon-bedrock/arn:aws:bedrock:…:application-inference-profile/ykpc0bdu467l`),
 * so the old "last path segment" display rendered AWS's opaque random
 * suffix in the agent sidebar. The authoritative display names live in the
 * gateway config's models catalog (`models.providers[*].models[]`, each
 * entry `{id, name}` — e.g. "Claude Opus 4.6"); resolve through that, and
 * when a model isn't in the catalog, hide ARN-shaped ids entirely rather
 * than show garbage.
 */

/** Walk `config.models.providers` and map every model id (bare AND
 *  provider-prefixed) to its display name. Tolerates any malformed shape —
 *  the config is runtime data from the container, not a typed contract. */
export function buildModelNameMap(config: unknown): Record<string, string> {
  const names: Record<string, string> = {};
  const models = (config as { models?: unknown } | null | undefined)?.models;
  const providers = (models as { providers?: unknown } | null | undefined)?.providers;
  if (!providers || typeof providers !== "object") return names;
  for (const [providerKey, provider] of Object.entries(providers as Record<string, unknown>)) {
    const list = (provider as { models?: unknown } | null | undefined)?.models;
    if (!Array.isArray(list)) continue;
    for (const entry of list) {
      const id = (entry as { id?: unknown } | null | undefined)?.id;
      const name = (entry as { name?: unknown } | null | undefined)?.name;
      if (typeof id !== "string" || typeof name !== "string" || !id || !name) continue;
      names[id] = name;
      names[`${providerKey}/${id}`] = name;
    }
  }
  return names;
}

export type CatalogModel = {
  /** Provider-prefixed ref ("amazon-bedrock/arn:…") — the form model fields
   *  (agents.defaults.model, heartbeat.model, agents.list[*].model) expect. */
  ref: string;
  /** Bare catalog id as listed under the provider. */
  bareId: string;
  /** Display name ("Claude Opus 4.6"). */
  name: string;
};

/** Flatten `config.models.providers` into selectable models. The catalog is
 *  the ONLY legitimate option source for routing dropdowns: per-owner IAM
 *  scopes Bedrock invokes to exactly these AIPs, so anything outside the
 *  catalog would fail closed at AccessDenied anyway. */
export function listCatalogModels(config: unknown): CatalogModel[] {
  const out: CatalogModel[] = [];
  const models = (config as { models?: unknown } | null | undefined)?.models;
  const providers = (models as { providers?: unknown } | null | undefined)?.providers;
  if (!providers || typeof providers !== "object") return out;
  for (const [providerKey, provider] of Object.entries(providers as Record<string, unknown>)) {
    const list = (provider as { models?: unknown } | null | undefined)?.models;
    if (!Array.isArray(list)) continue;
    for (const entry of list) {
      const id = (entry as { id?: unknown } | null | undefined)?.id;
      const name = (entry as { name?: unknown } | null | undefined)?.name;
      if (typeof id !== "string" || !id) continue;
      out.push({
        ref: `${providerKey}/${id}`,
        bareId: id,
        name: typeof name === "string" && name ? name : id,
      });
    }
  }
  return out;
}

/**
 * Resolve a model id to a display name, or null when nothing presentable
 * exists (callers should render nothing rather than an opaque ARN tail).
 */
export function displayModelName(
  modelStr: string | undefined | null,
  names?: Record<string, string>,
): string | null {
  if (!modelStr) return null;
  if (names?.[modelStr]) return names[modelStr];
  // Strip the provider prefix ("amazon-bedrock/arn:aws:…" → "arn:aws:…")
  // and retry — agents.list carries the prefixed form, the catalog the
  // bare. A BARE ARN must not be stripped: its first "/" is inside the
  // ARN ("…inference-profile/<suffix>"), not a provider separator.
  const slash = modelStr.indexOf("/");
  const bare = slash >= 0 && !modelStr.startsWith("arn:") ? modelStr.slice(slash + 1) : modelStr;
  if (names?.[bare]) return names[bare];
  // Unresolvable ARN: the last segment is an opaque random resource id
  // (e.g. "ykpc0bdu467l") — showing it is worse than showing nothing.
  if (bare.startsWith("arn:")) return null;
  // Legacy readable ids ("us.anthropic.claude-opus-4-6-v1:0"): keep the old
  // last-segment + version-strip behavior.
  return modelStr.split("/").pop()?.replace(/-v\d+:\d+$/, "") || modelStr;
}
