// apps/frontend/src/components/teams/agents/AgentManageSection.tsx
"use client";

import { useState } from "react";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { EditorialButton } from "@/components/teams/shell";
import { ReportsToPicker } from "@/components/teams/agents/ReportsToPicker";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";

/** Seeded = Isol8 provisioning contract (Friday/Atlas). Mirror of the server
 *  rule in core/services/paperclip_openclaw_pairing.is_seeded_agent. */
export function isSeededAgent(a: CompanyAgent): boolean {
  return a.role === "ceo" || a.name === "Atlas";
}

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
}: {
  agent: CompanyAgent;
  agents: CompanyAgent[];
}) {
  const { setReportsTo, pause, resume, remove } = useAgentMutations();
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const seeded = isSeededAgent(agent);
  const paused = agent.status === "paused";

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    try { await fn(); } finally { setBusy(false); }
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Read-only meta — edited in the agent's control panel */}
      <section className="editorial-card p-4 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className="font-editorial-serif text-lg">Profile</h3>
          <a
            href={`/chat?agent=${agent.id}`}
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

      {/* Reporting line — editable here */}
      <section className="editorial-card p-4 flex flex-col gap-2">
        <h3 className="font-editorial-serif text-lg">Reports to</h3>
        <ReportsToPicker
          agents={agents}
          agentId={agent.id}
          value={agent.reportsTo}
          disabled={busy}
          onChange={(managerId) => run(() => setReportsTo(agent.id, managerId))}
        />
      </section>

      {/* Lifecycle actions */}
      <section className="flex items-center gap-2">
        {paused ? (
          <EditorialButton variant="secondary" size="sm" disabled={busy}
            onClick={() => run(() => resume(agent.id))}>
            Resume
          </EditorialButton>
        ) : (
          <EditorialButton variant="secondary" size="sm" disabled={busy}
            onClick={() => run(() => pause(agent.id))}>
            Pause
          </EditorialButton>
        )}
        {!seeded && !confirmDelete && (
          <EditorialButton variant="secondary" size="sm" disabled={busy}
            onClick={() => setConfirmDelete(true)}>
            Delete
          </EditorialButton>
        )}
        {!seeded && confirmDelete && (
          <>
            <span className="text-sm text-[var(--danger)]">Delete {agent.name}?</span>
            <EditorialButton size="sm" disabled={busy}
              onClick={() => run(() => remove(agent.id))}>
              Confirm
            </EditorialButton>
            <EditorialButton variant="secondary" size="sm" disabled={busy}
              onClick={() => setConfirmDelete(false)}>
              Cancel
            </EditorialButton>
          </>
        )}
      </section>
    </div>
  );
}
