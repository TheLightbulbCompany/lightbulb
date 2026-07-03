// apps/frontend/src/components/teams/agents/AgentManageSection.tsx
"use client";

import type { CompanyAgent } from "@/components/teams/shared/types";
import { AgentOrgControls } from "@/components/teams/agents/AgentOrgControls";

// `isSeededAgent` now lives alongside the org controls that gate on it.
// Re-exported here so existing importers (AgentsListPanel + tests) keep their
// `@/components/teams/agents/AgentManageSection` import path.
export { isSeededAgent } from "@/components/teams/agents/AgentOrgControls";

function MetaRow({ label, value }: { label: string; value?: string | null }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs uppercase tracking-wide text-[var(--mute)]">{label}</span>
      <span className="text-[var(--ink)]">{value?.trim() ? value : "—"}</span>
    </div>
  );
}

export function AgentManageSection({
  agent,
  agents,
  onDeleted,
}: {
  agent: CompanyAgent;
  agents: CompanyAgent[];
  /** Called after a successful delete. The detail view passes this to
   *  navigate away — remove() clears the agent's detail cache, so staying on
   *  /teams/agents/{id} would render a permanent "Loading…". */
  onDeleted?: () => void;
}) {
  return (
    <div className="flex flex-col gap-6">
      {/* Read-only meta — edited in the agent's control panel */}
      <section className="editorial-card p-4 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className="font-editorial-serif text-lg">Profile</h3>
          <a
            href={`/chat?panel=agents&agent=${agent.id}`}
            onClick={(e) => {
              // Plain left-click swaps the shell panel client-side (this
              // section only renders inside the /chat shell); modified
              // clicks keep the href's native new-tab semantics. A bare
              // href would trigger a full document reload.
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
              e.preventDefault();
              window.history.pushState(null, "", `/chat?panel=agents&agent=${agent.id}`);
            }}
            className="text-sm hover:underline"
            style={{ color: "var(--forest)" }}
          >
            Edit in control panel →
          </a>
        </div>
        <div className="grid grid-cols-2 gap-4">
          <MetaRow label="Role" value={agent.role} />
          <MetaRow label="Title" value={agent.title} />
          <MetaRow label="Icon" value={agent.icon} />
          <MetaRow label="Status" value={agent.status} />
        </div>
        <MetaRow label="Capabilities" value={agent.capabilities} />
        <p className="text-xs text-[var(--mute)]">
          Role, title, capabilities and icon are managed in the agent&apos;s control panel.
        </p>
      </section>

      {/* Reporting line + lifecycle — shared with the /chat inspector. */}
      <AgentOrgControls agent={agent} agents={agents} onDeleted={onDeleted} />
    </div>
  );
}
