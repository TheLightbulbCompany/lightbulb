// Pure decision core of WorkspaceShell's URL reconciler, extracted so the
// ordering subtleties are testable without a shell render
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
  MESSAGES_PANEL_ID,
  ONE_SHOT_PARAMS,
  NON_COMMAND_PARAMS,
} from "./panelRegistry";

export interface ReconcilerDecision {
  // The value to latch into lastApplied — already recomputed for the
  // post-strip URL when strippedSearch is non-null.
  nextSignature: string;
  // null = the URL carries no command (still latch nextSignature).
  command: WorkspaceCommand | null;
  // The RAW ?panel value for openPanel; null for chat commands (chat is opened
  // via openChatPanel with the descriptor's agentId). Deliberately NOT
  // alias-resolved: describePanel resolves the alias itself AND branches on the
  // pre-alias key to carry a retired panel's meaning onto its successor
  // (?panel=teams-org-chart must land on the Agents CHART view,
  // panelRegistry.ts). Handing
  // it a pre-resolved key silently drops that — the whole reason a deep link to
  // a retired panel used to open its successor's default tab.
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
  // Preserve the existing roster-readiness gate: deferred chat commands are
  // neither latched nor stripped until the shell reports a settled roster.
  agentsSettled = true,
): ReconcilerDecision | null {
  const sig = commandSignature(search);
  if (sig === lastApplied) return null;

  const command = parseCommand(search);
  if (!command) {
    return { nextSignature: sig, command: null, panelKey: null, durableParams: {}, strippedSearch: null };
  }

  if (command.descriptor.component === "chat" && !agentsSettled) return null;

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

  const panelKey = command.descriptor.component === "chat" ? null : params.get("panel");

  return { nextSignature, command, panelKey, durableParams, strippedSearch };
}

// Whether executing `command` will focus an existing panel rather than open a
// new one — analytics-only (workspace_panel_opened fires for NEW panels).
// All agent commands target the singleton Messages panel.
export function isCommandTargetOpen(
  command: WorkspaceCommand,
  openPanelIds: readonly string[],
): boolean {
  const { descriptor } = command;
  if (descriptor.component !== "chat") return openPanelIds.includes(descriptor.panelId);
  return openPanelIds.includes(MESSAGES_PANEL_ID);
}
