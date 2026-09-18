"use client";

import { AlertCircle } from "lucide-react";

/**
 * Per-agent tool access. Hermes gates by TOOLSET, not per tool, so there is no
 * allow/deny list to edit — and the editor was worse than empty: Save called
 * `agents.update {tools}`, which answered `{"ok": true}` and wrote nothing.
 * The backend refuses that call; this is the surface that stops asking for it.
 *
 * Takes `agentId` so the tab's call site (AgentsPanel / AgentInspectorPanel)
 * needs no special case, and so a toolset editor can grow back here later.
 */
export function AgentToolsTab({ agentId: _agentId }: { agentId: string }) {
  return (
    <div className="mt-4 flex items-start gap-2 px-3 py-2 rounded-lg bg-cream border border-[var(--cream-deep)]">
      <AlertCircle size={12} strokeWidth={1.75} className="text-[var(--midnight-muted)] shrink-0 mt-0.5" />
      <p className="text-xs text-[var(--midnight-muted)]">
        Per-tool access isn&apos;t available on this runtime. Tools are granted by toolset here,
        not one by one.
      </p>
    </div>
  );
}
