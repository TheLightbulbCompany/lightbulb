/**
 * Agent types + pure helpers shared by web and mobile. The mutation-bearing
 * `useAgents` hook stays web-side for now (it couples to PostHog analytics);
 * both platforms read the same `agents.list` response shape though, so the
 * contract lives here.
 */

/**
 * The gateway's `agents.list` RPC returns the agent's `model` field in one
 * of two shapes depending on whether the agent has an override:
 *
 *  - legacy flat string: `"amazon-bedrock/qwen.qwen3-vl-235b-a22b"`
 *  - structured object:  `{ primary: "amazon-bedrock/...", fallbacks: [...] }`
 *
 * Pre-OpenClaw-4.5 flattened this to a string in RPC responses; 4.5 returns
 * the structured shape as-is, which blew up anywhere on the frontend that
 * was calling `agent.model.split(...)` directly. Call sites MUST use
 * `getAgentModelString` to normalize.
 */
export type AgentModelRef = string | { primary?: string; fallbacks?: string[] };

export interface Agent {
  id: string;
  name?: string;
  identity?: { name?: string; emoji?: string; avatar?: string };
  model?: AgentModelRef;
}

export interface AgentsListResponse {
  defaultId?: string;
  agents?: Agent[];
}

/**
 * On an AgentCore (Hermes) workspace there are no model refs on the wire at all: the roster
 * intercept reports a closed TIER vocabulary, and `agents.update {model}` accepts only those
 * two values (`runtime_rpc._AGENTCORE_MODEL_TIERS`). A raw ref could point outside the owner's
 * own Bedrock grant, so one is never stored. These are the display labels for them — no ECS
 * model ref collides, since every ECS ref is `provider/model`-shaped.
 */
const MODEL_TIER_LABELS: Readonly<Record<string, string>> = {
  standard: "Standard",
  premium: "Premium",
};

/** Normalize an `Agent.model` field to a plain string (or undefined). */
export function getAgentModelString(agent: Pick<Agent, "model"> | undefined): string | undefined {
  const model = agent?.model;
  const ref =
    typeof model === "string"
      ? model.trim()
      : model && typeof model === "object"
        ? model.primary?.trim()
        : undefined;
  if (!ref) return undefined;
  return MODEL_TIER_LABELS[ref] ?? ref;
}

/** Resolve the human-readable label for an agent. Prefers ``identity.name``
 * (the user-visible name OpenClaw renders in its own UI), falls back to the
 * top-level ``name`` field, and only as a last resort surfaces the raw
 * ``id`` (e.g. ``agent_e7668b0dd6a6`` from a freshly-deployed catalog
 * template before the user has renamed it). */
export function agentDisplayName(
  agent: Pick<Agent, "id" | "name" | "identity">,
): string {
  return agent.identity?.name || agent.name || agent.id;
}
