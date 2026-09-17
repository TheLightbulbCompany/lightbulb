/**
 * Pure builders + readers for the model-routing config surfaces.
 *
 * Kept free of React so the patch shapes — which the gateway deep-merges
 * into openclaw.json — are unit-testable. Shapes follow OpenClaw's
 * agent-defaults schema: `agents.defaults.heartbeat` carries the global
 * heartbeat (every/model/activeHours), and each `agents.list[*]` entry may
 * carry the same block as a per-agent override (types.agents.ts). A `null`
 * in a merge patch deletes the key (RFC 7386), which is how an override
 * reverts to "inherit".
 */

export type HeartbeatSettings = {
  /** Duration string ("30m", "2h"). undefined = inherit. */
  every?: string;
  /** Provider-prefixed model ref. undefined = inherit. */
  model?: string;
  /** Local-time window; heartbeats run only inside it. undefined = always. */
  activeHours?: { start: string; end: string };
};

export const HEARTBEAT_CADENCES = ["15m", "30m", "1h", "2h", "4h", "8h", "24h"] as const;

type HeartbeatBlock = {
  every?: unknown;
  model?: unknown;
  activeHours?: { start?: unknown; end?: unknown } | null;
};

function readHeartbeatBlock(block: unknown): HeartbeatSettings {
  const hb = (block ?? {}) as HeartbeatBlock;
  const out: HeartbeatSettings = {};
  if (typeof hb.every === "string" && hb.every) out.every = hb.every;
  if (typeof hb.model === "string" && hb.model) out.model = hb.model;
  const ah = hb.activeHours;
  if (ah && typeof ah.start === "string" && typeof ah.end === "string" && ah.start && ah.end) {
    out.activeHours = { start: ah.start, end: ah.end };
  }
  return out;
}

/** The agent's own heartbeat overrides (NOT merged with defaults — the UI
 *  shows inherit placeholders for unset fields). */
export function readAgentHeartbeat(config: unknown, agentId: string): HeartbeatSettings {
  const agents = (config as { agents?: { list?: unknown } } | null | undefined)?.agents;
  const list = agents && Array.isArray(agents.list) ? agents.list : [];
  const entry = list.find(
    (a) => (a as { id?: unknown } | null | undefined)?.id === agentId,
  ) as { heartbeat?: unknown } | undefined;
  return readHeartbeatBlock(entry?.heartbeat);
}

/** The global defaults — used for "inherit (…)" placeholders. */
export function readDefaultHeartbeat(config: unknown): HeartbeatSettings {
  const defaults = (config as { agents?: { defaults?: { heartbeat?: unknown } } } | null | undefined)
    ?.agents?.defaults;
  return readHeartbeatBlock(defaults?.heartbeat);
}

export function readDefaultModelPrimary(config: unknown): string | undefined {
  const model = (config as { agents?: { defaults?: { model?: unknown } } } | null | undefined)
    ?.agents?.defaults?.model;
  if (typeof model === "string") return model || undefined;
  const primary = (model as { primary?: unknown } | null | undefined)?.primary;
  return typeof primary === "string" && primary ? primary : undefined;
}

/**
 * Merge patch for one agent's heartbeat override. Unset fields become
 * `null` so an existing override actually reverts to inherit instead of
 * lingering; an entirely-inherit selection nulls the whole block.
 * `agents.list` merges by id, so sibling agents are untouched.
 */
export function buildAgentHeartbeatPatch(
  agentId: string,
  settings: HeartbeatSettings,
): Record<string, unknown> {
  const allInherit = !settings.every && !settings.model && !settings.activeHours;
  const heartbeat = allInherit
    ? null
    : {
        every: settings.every ?? null,
        model: settings.model ?? null,
        activeHours: settings.activeHours
          ? { start: settings.activeHours.start, end: settings.activeHours.end }
          : null,
      };
  return { agents: { list: [{ id: agentId, heartbeat }] } };
}

/**
 * Merge patch for the default (primary) model. Fallbacks are preserved by
 * NOT touching them — the merge patch only carries `primary`.
 */
export function buildDefaultModelPatch(primaryRef: string): Record<string, unknown> {
  return { agents: { defaults: { model: { primary: primaryRef } } } };
}

/**
 * Merge patch routing ALL pure-overhead work (history compaction,
 * memory-flush, the default heartbeat model) to one model. These three are
 * deliberately one knob: they're the same "doesn't need the premium tier"
 * cost decision, and splitting them invites misconfiguration.
 */
export function buildOverheadModelPatch(modelRef: string): Record<string, unknown> {
  return {
    agents: {
      defaults: {
        compaction: { model: modelRef, memoryFlush: { model: modelRef } },
        heartbeat: { model: modelRef },
      },
    },
  };
}

export function readOverheadModel(config: unknown): string | undefined {
  const defaults = (config as { agents?: { defaults?: { compaction?: { model?: unknown } } } } | null | undefined)
    ?.agents?.defaults;
  const m = defaults?.compaction?.model;
  return typeof m === "string" && m ? m : undefined;
}

/* --------------------------------------------------------------------------
 * AgentCore (Hermes). `agents.defaults.*` does not exist there — the overhead
 * work is the `auxiliary.<task>` block, one entry per Hermes auxiliary task
 * (compression, curator, title_generation, …). M6 task 10-C, scan rows 4/23.
 * ------------------------------------------------------------------------ */

/** The pseudo-key the intercept expands. The browser sends ONE id; the backend rewrites it
 *  into every `auxiliary.<task>` entry with the provider `hermes_profiles.model_block`
 *  derives, and never stores the key itself. The task list and the provider are the backend
 *  writer's to know — a hand-built block here would drift from the one provisioning writes. */
export function buildHermesOverheadModelPatch(modelId: string): Record<string, unknown> {
  return { auxiliary: { model: modelId } };
}

/** The model every auxiliary task is pinned to, read back off the profile. Takes the first
 *  task entry that carries one: we always write them together, and a partially-written block
 *  should still show something rather than an empty select. */
export function readHermesOverheadModel(config: unknown): string | undefined {
  const auxiliary = (config as { auxiliary?: unknown } | null | undefined)?.auxiliary;
  if (!auxiliary || typeof auxiliary !== "object") return undefined;
  for (const entry of Object.values(auxiliary as Record<string, unknown>)) {
    const model = (entry as { model?: unknown } | null | undefined)?.model;
    if (typeof model === "string" && model) return model;
  }
  return undefined;
}
