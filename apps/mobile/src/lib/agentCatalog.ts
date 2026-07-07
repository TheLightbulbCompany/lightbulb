/**
 * Pure helpers for the Agents control panel — model catalog resolution and
 * name validation. Ported verbatim (behavior-for-behavior) from the web
 * frontend so mobile agent edits produce the same refs the gateway expects:
 *   - `lib/modelDisplay.ts` (buildModelNameMap / displayModelName /
 *     listCatalogModels)
 *   - `lib/agentIdentity.ts` (normalizeAgentId / validateAgentName)
 *
 * The gateway `config.get` payload is untyped runtime data from the
 * container, so every walker below tolerates any malformed shape.
 */

// ---------------------------------------------------------------------------
// Model catalog (mirrors web lib/modelDisplay.ts)
// ---------------------------------------------------------------------------

export interface CatalogModel {
  /** Provider-prefixed ref ("amazon-bedrock/arn:…") — what the agent model
   *  field (agents.list[*].model, agents.update {model}) expects. */
  ref: string;
  /** Display name ("Claude Opus 4.6"). */
  name: string;
}

/** Walk `config.models.providers` and map every model id (bare AND
 *  provider-prefixed) to its display name. */
export function buildModelNameMap(config: unknown): Record<string, string> {
  const names: Record<string, string> = {};
  const models = (config as { models?: unknown } | null | undefined)?.models;
  const providers = (models as { providers?: unknown } | null | undefined)
    ?.providers;
  if (!providers || typeof providers !== "object") return names;
  for (const [providerKey, provider] of Object.entries(
    providers as Record<string, unknown>,
  )) {
    const list = (provider as { models?: unknown } | null | undefined)?.models;
    if (!Array.isArray(list)) continue;
    for (const entry of list) {
      const id = (entry as { id?: unknown } | null | undefined)?.id;
      const name = (entry as { name?: unknown } | null | undefined)?.name;
      if (typeof id !== "string" || typeof name !== "string" || !id || !name)
        continue;
      names[id] = name;
      names[`${providerKey}/${id}`] = name;
    }
  }
  return names;
}

/** Flatten `config.models.providers` into selectable models. The catalog is
 *  the ONLY legitimate option source — per-owner IAM scopes Bedrock invokes
 *  to exactly these profiles, so anything outside would fail closed. */
export function listCatalogModels(config: unknown): CatalogModel[] {
  const out: CatalogModel[] = [];
  const models = (config as { models?: unknown } | null | undefined)?.models;
  const providers = (models as { providers?: unknown } | null | undefined)
    ?.providers;
  if (!providers || typeof providers !== "object") return out;
  for (const [providerKey, provider] of Object.entries(
    providers as Record<string, unknown>,
  )) {
    const list = (provider as { models?: unknown } | null | undefined)?.models;
    if (!Array.isArray(list)) continue;
    for (const entry of list) {
      const id = (entry as { id?: unknown } | null | undefined)?.id;
      const name = (entry as { name?: unknown } | null | undefined)?.name;
      if (typeof id !== "string" || !id) continue;
      out.push({
        ref: `${providerKey}/${id}`,
        name: typeof name === "string" && name ? name : id,
      });
    }
  }
  return out;
}

/** Resolve a model ref to a display name, or null when nothing presentable
 *  exists (render nothing rather than an opaque ARN tail). */
export function displayModelName(
  modelStr: string | undefined | null,
  names?: Record<string, string>,
): string | null {
  if (!modelStr) return null;
  if (names?.[modelStr]) return names[modelStr];
  const slash = modelStr.indexOf("/");
  const bare =
    slash >= 0 && !modelStr.startsWith("arn:") ? modelStr.slice(slash + 1) : modelStr;
  if (names?.[bare]) return names[bare];
  if (bare.startsWith("arn:")) return null;
  return modelStr.split("/").pop()?.replace(/-v\d+:\d+$/, "") || modelStr;
}

/**
 * Build the selectable model set for the picker: catalog models, plus any
 * `agents.defaults.models` alias-map entries not already covered, plus the
 * agent's current ref pinned to the front when it is not otherwise present
 * (so an off-catalog override is still shown + reselectable). Mirrors the
 * web AgentOverviewTab selectorModels derivation.
 */
export function buildSelectorModels(
  config: unknown,
  currentRef: string | undefined,
): CatalogModel[] {
  const names = buildModelNameMap(config);
  const out = listCatalogModels(config);
  const seen = new Set(out.map((m) => m.ref));

  const aliasMap = (
    config as { agents?: { defaults?: { models?: unknown } } } | null | undefined
  )?.agents?.defaults?.models;
  if (aliasMap && typeof aliasMap === "object") {
    for (const [id, entry] of Object.entries(aliasMap as Record<string, unknown>)) {
      if (seen.has(id)) continue;
      const alias = (entry as { alias?: unknown } | null | undefined)?.alias;
      out.push({
        ref: id,
        name:
          typeof alias === "string" && alias
            ? alias
            : displayModelName(id, names) || id,
      });
      seen.add(id);
    }
  }

  if (currentRef && !seen.has(currentRef)) {
    out.unshift({
      ref: currentRef,
      name: displayModelName(currentRef, names) || currentRef,
    });
  }
  return out;
}

// ---------------------------------------------------------------------------
// Name validation (mirrors web lib/agentIdentity.ts)
// ---------------------------------------------------------------------------

/** Slugify an agent name into OpenClaw's `normalizeAgentId` shape. NOT the
 *  id the backend assigns (Paperclip mints a UUID) — used only as a stable
 *  key for client-side name-uniqueness checks. */
export function normalizeAgentId(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

/** Names the system reserves and rejects on create. */
export const RESERVED_AGENT_IDS: readonly string[] = ["main"];

/** Validate a proposed name against existing display names. Returns `null`
 *  (valid) or a user-facing error string. Empty name is "not yet typed",
 *  not an error — caller gates on `name.trim() !== ""`. */
export function validateAgentName(
  name: string,
  existingNames: string[],
): string | null {
  const id = normalizeAgentId(name);
  if (id === "") return null;
  const existingSlugs = existingNames.map(normalizeAgentId);
  if (existingSlugs.includes(id)) return "An agent with this name already exists";
  if (RESERVED_AGENT_IDS.includes(id)) return "This name is reserved";
  return null;
}

/** Derive the explicit create-time workspace path. OpenClaw's agents.create
 *  requires a non-empty `workspace` (it does NOT inherit
 *  agents.defaults.workspace at create time), so we fill the same path the
 *  default resolves to at runtime. */
export function deriveWorkspace(name: string): string {
  return `.openclaw/workspaces/${normalizeAgentId(name)}`;
}
