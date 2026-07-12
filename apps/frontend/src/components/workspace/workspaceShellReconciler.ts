// Pure decision core of WorkspaceShell's URL reconciler, extracted so the
// ordering subtleties are testable without a dockview render
// (workspaceShellUrl.test.ts pins them).
//
// The ordering contract (spec §6):
//   1. Latch FIRST: compare commandSignature(search) against lastApplied and
//      bail on a match — non-command churn (billing returns, utm params being
//      stripped elsewhere) must never re-execute a command.
//   2. One-shots ARE part of the signature, so after stripping them from the
//      URL the latch must be RECOMPUTED against the post-strip search —
//      otherwise the strip's own searchParams churn would re-execute the
//      command (this time without its one-shots).
import {
  parseCommand,
  commandSignature,
  stripOneShots,
  type WorkspaceCommand,
} from "./urlChannel";
import {
  resolvePanelAlias,
  chatPanelId,
  DEFAULT_AGENT_SENTINEL,
  ONE_SHOT_PARAMS,
  NON_COMMAND_PARAMS,
} from "./panelRegistry";

export interface ReconcilerDecision {
  // The value to latch into lastApplied — already recomputed for the
  // post-strip URL when strippedSearch is non-null.
  nextSignature: string;
  // null = the URL carries no command (still latch nextSignature).
  command: WorkspaceCommand | null;
  // Alias-resolved raw ?panel value for openPanel; null for chat commands
  // (chat is opened via openChatPanel with the descriptor's agentId).
  panelKey: string | null;
  // Raw URL-grammar durable params (e.g. { agent, inspectorTab }) — openPanel
  // re-derives the descriptor from these; one-shots and billing params removed.
  durableParams: Record<string, string>;
  // Non-null → the shell must replaceSearchParams with this after executing.
  strippedSearch: string | null;
}

export function deriveReconcilerAction(
  search: string,
  lastApplied: string | null,
): ReconcilerDecision | null {
  const sig = commandSignature(search);
  if (sig === lastApplied) return null;

  const command = parseCommand(search);
  if (!command) {
    return { nextSignature: sig, command: null, panelKey: null, durableParams: {}, strippedSearch: null };
  }

  const strippedSearch = stripOneShots(search);
  const nextSignature = strippedSearch !== null ? commandSignature(strippedSearch) : sig;

  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const durableParams: Record<string, string> = {};
  params.forEach((v, k) => {
    durableParams[k] = v;
  });
  delete durableParams.panel;
  for (const k of ONE_SHOT_PARAMS) delete durableParams[k];
  for (const k of NON_COMMAND_PARAMS) delete durableParams[k];

  const rawPanelKey = params.get("panel");
  const panelKey =
    command.descriptor.component === "chat"
      ? null
      : rawPanelKey
        ? resolvePanelAlias(rawPanelKey)
        : null;

  return { nextSignature, command, panelKey, durableParams, strippedSearch };
}

// Whether executing `command` will focus an existing panel rather than open a
// new one — analytics-only (workspace_panel_opened fires for NEW panels).
// Mirrors openChatPanel's sentinel unification: the Mission Control sentinel
// panel durably means "the default agent", so a command naming the concrete
// default agent is "already open" when the sentinel is.
export function isCommandTargetOpen(
  command: WorkspaceCommand,
  openPanelIds: readonly string[],
  resolveDefaultAgentId: () => string | undefined,
): boolean {
  const { descriptor } = command;
  if (descriptor.component !== "chat") return openPanelIds.includes(descriptor.panelId);
  const agentId = descriptor.params.agentId;
  if (openPanelIds.includes(chatPanelId(agentId))) return true;
  const resolved = resolveDefaultAgentId();
  if (agentId === DEFAULT_AGENT_SENTINEL) {
    return resolved !== undefined && openPanelIds.includes(chatPanelId(resolved));
  }
  return resolved === agentId && openPanelIds.includes(chatPanelId(DEFAULT_AGENT_SENTINEL));
}
