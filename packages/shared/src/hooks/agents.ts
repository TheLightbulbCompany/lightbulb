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
  /** The display name for `model`, when the runtime can give it away for free. AgentCore fills
   *  it in (`agents.list` already reads every profile), so a roster surface never has to dial
   *  `models.list` — which on a BYO/OAuth workspace is a ~55s microVM cold start — for a label
   *  (M6-R53). Absent on ECS, where the gateway config's catalog is already in hand. */
  modelName?: string;
  /** Paperclip roster fields an AgentCore `agents.list` row carries (the roster IS the row
   *  there — there is no `agent.identity.get` on this runtime). */
  role?: string;
  title?: string;
  capabilities?: string;
  icon?: string;
}

export interface AgentsListResponse {
  defaultId?: string;
  agents?: Agent[];
}

// No `models.list` response type lives here: every client walks that payload with the local
// shape-detection in `modelDisplay.ts`/`agentCatalog.ts`, so a shared interface would be a
// public API with zero importers.

/**
 * Normalize an `Agent.model` field to a plain string (or undefined).
 *
 * Every runtime now reports a real model ID here — on AgentCore it is the profile's own
 * `model.default` (a Bedrock application-inference-profile ARN, or a Hermes catalog id on
 * BYO/OAuth). The transitional `"standard"`/`"premium"` tier words this used to translate are
 * gone (M6 task 10-C, M6-R50): a tier word named no model the owner could look up, and the
 * picker now sends the same ids `models.list` offers. Resolve the ID to a display name through
 * that list (`buildModelNameMap`), not here.
 */
export function getAgentModelString(agent: Pick<Agent, "model"> | undefined): string | undefined {
  const model = agent?.model;
  const ref =
    typeof model === "string"
      ? model.trim()
      : model && typeof model === "object"
        ? model.primary?.trim()
        : undefined;
  return ref || undefined;
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
