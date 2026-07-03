// Canonical in-shell URL for a single agent. Agents have no teams-* detail
// panel — the per-agent surface is the shell's agent inspector, addressed by
// the plain ControlPanelRouter key `agent-inspector` plus the agent id in
// ?agent (the same params the shell's own roster clicks commit).
export function agentInspectorUrl(agentId: string): string {
  return `/chat?panel=agent-inspector&agent=${encodeURIComponent(agentId)}`;
}
