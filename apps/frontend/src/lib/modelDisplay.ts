/**
 * Human-readable model names for agent surfaces.
 *
 * Bedrock model ids are per-owner application-inference-profile ARNs
 * (`amazon-bedrock/arn:aws:bedrock:…:application-inference-profile/ykpc0bdu467l`),
 * so the old "last path segment" display rendered AWS's opaque random
 * suffix in the agent sidebar. Resolve through the owner's model list
 * instead, and when a model isn't in it, hide ARN-shaped ids entirely
 * rather than show garbage.
 *
 * Two sources carry that list, and every walker below takes EITHER (M6 task
 * 10-C, scan rows 2/27): on ECS the gateway config's catalog
 * (`{models: {providers: {…: {models: [{id, name}]}}}}`), on AgentCore the
 * `models.list` RPC (`{models: [{id, name}], defaultId}`) — the owner's own
 * Bedrock inference profiles, or Hermes's provider catalog on BYO/OAuth. A
 * Hermes config has no `models` key at all, so there is no third shape and
 * nothing to branch on beyond "is it an array".
 */

/** The `models.list` entries, when `source` is that payload rather than a gateway config. */
function runtimeModelList(source: unknown): { id: string; name?: string }[] | null {
  const models = (source as { models?: unknown } | null | undefined)?.models;
  return Array.isArray(models)
    ? (models.filter(
        (entry) => typeof (entry as { id?: unknown } | null)?.id === "string" && (entry as { id: string }).id,
      ) as { id: string; name?: string }[])
    : null;
}

/** Map every model id (bare AND provider-prefixed) to its display name. Tolerates any
 *  malformed shape — both sources are runtime data, not a typed contract. */
export function buildModelNameMap(config: unknown): Record<string, string> {
  const names: Record<string, string> = {};
  const runtimeModels = runtimeModelList(config);
  if (runtimeModels) {
    for (const entry of runtimeModels) {
      if (typeof entry.name === "string" && entry.name) names[entry.id] = entry.name;
    }
    return names;
  }
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

/** Flatten either source into selectable models. The list is the ONLY legitimate option
 *  source for routing dropdowns: per-owner IAM scopes Bedrock invokes to exactly these AIPs,
 *  so anything outside it would fail closed at AccessDenied anyway — and on AgentCore the
 *  backend refuses an id it did not offer before it can reach a profile. */
export function listCatalogModels(config: unknown): CatalogModel[] {
  const out: CatalogModel[] = [];
  const runtimeModels = runtimeModelList(config);
  if (runtimeModels) {
    // `models.list` ids are already the exact ref the picker sends back — no provider prefix
    // to rebuild (a Hermes profile stores `model.default` bare, with `model.provider` beside it).
    return runtimeModels.map((entry) => ({ ref: entry.id, bareId: entry.id, name: entry.name || entry.id }));
  }
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
