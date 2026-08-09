"use client";

import { useMemo, useState } from "react";
import { Bot } from "lucide-react";
import {
  EditorialButton,
  EditorialDialog,
  PanelDataTable,
  PanelEmptyState,
  PanelMasthead,
  PanelToolbar,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { AgentCreateChooser } from "@/components/agents/AgentCreateChooser";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick } from "@/lib/shellNav";
import { agentInspectorUrl } from "@/components/teams/shared/lib/agentInspectorUrl";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";
import { isSeededAgent } from "@/components/teams/agents/AgentManageSection";
import type { CompanyAgent } from "@/components/teams/shared/types";

interface Agent {
  id: string;
  name: string;
  role: string;
  status?: string;
}

export function AgentsListPanel() {
  const { read } = useTeamsApi();
  const { pause, resume, remove } = useAgentMutations();
  const { data, isLoading, error, mutate } = read<{ agents: Agent[] }>(
    "/agents",
  );
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");

  const agents = data?.agents;
  const filtered = useMemo(() => {
    const list = agents ?? [];
    return search
      ? list.filter((a) =>
          (a.name + " " + a.role).toLowerCase().includes(search.toLowerCase()),
        )
      : list;
  }, [agents, search]);

  if (error) {
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );
  }

  const columns: PanelDataColumn<Agent>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      cell: (a) => (
        <div className="font-medium text-[var(--ink)]">{a.name}</div>
      ),
    },
    {
      key: "role",
      header: "Role",
      cell: (a) => (
        <span className="text-xs uppercase tracking-wide text-[var(--mute)]">
          {a.role}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (a) => {
        const seeded = isSeededAgent(a as unknown as CompanyAgent);
        const paused = a.status === "paused";
        return (
          <span className="flex items-center justify-end gap-2">
            <button
              className="text-sm hover:underline text-[var(--ink-soft)]"
              onClick={() => (paused ? resume(a.id) : pause(a.id))}
            >
              {paused ? "Resume" : "Pause"}
            </button>
            {!seeded && (
              <button
                className="text-sm hover:underline text-[var(--danger)]"
                onClick={() => {
                  if (window.confirm(`Delete ${a.name}? This removes the agent and its workspace.`)) {
                    remove(a.id);
                  }
                }}
              >
                Delete
              </button>
            )}
          </span>
        );
      },
    },
    {
      key: "open",
      header: "",
      align: "right",
      cell: (a) => (
        <a
          href={agentInspectorUrl(a.id)}
          onClick={(e) => handleShellLinkClick(e, agentInspectorUrl(a.id))}
          className="text-sm hover:underline"
          style={{ color: "var(--forest)" }}
        >
          Open →
        </a>
      ),
    },
  ];

  return (
    <div className="editorial-page flex flex-col gap-6 p-4 sm:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Team"
        title="Agents"
        actions={
          <EditorialButton variant="primary" onClick={() => setCreating(true)}>
            New agent
          </EditorialButton>
        }
      />
      <PanelToolbar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Find agent..."
        searchAriaLabel="Search agents"
      />
      {isLoading ? (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Loading…
        </div>
      ) : filtered.length === 0 ? (
        <PanelEmptyState
          icon={Bot}
          headline="No agents yet."
          supporting={
            search
              ? "No agents match your search."
              : "Spin up your first agent — assign a role + start delegating work."
          }
          action={{ label: "New agent", onClick: () => setCreating(true) }}
        />
      ) : (
        <PanelDataTable<Agent>
          rows={filtered}
          columns={columns}
          rowKey={(a) => a.id}
        />
      )}

      {creating && (
        <CreateAgentDialog
          agents={agents ?? []}
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            mutate();
          }}
        />
      )}
    </div>
  );
}

function CreateAgentDialog({
  agents,
  onClose,
  onCreated,
}: {
  /** Existing roster — the manager picker's candidates. A brand-new agent
   *  has no descendants, so every existing agent is an eligible manager. */
  agents: Agent[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  const { open: openMarketplace } = useMarketplaceDrawer();
  // Chooser first: steer toward hiring a pre-built marketplace agent; the
  // custom-build form is the secondary path.
  const [mode, setMode] = useState<"choose" | "build">("choose");
  const [name, setName] = useState("");
  const [role, setRole] = useState("engineer");
  const [title, setTitle] = useState("");
  const [reportsTo, setReportsTo] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setSubmitting(true);
    setErr(null);
    try {
      // SECURITY: body intentionally contains NO adapterType, NO adapterConfig,
      // NO url, NO authToken. The BFF synthesizes the openclaw_gateway adapter
      // server-side via core.services.paperclip_adapter_config and rejects any
      // smuggled fields with 422 (Pydantic extra="forbid"). Defense in depth:
      // the UI doesn't even render those inputs.
      //
      // Rich hire (A7): title + reportsTo are already accepted by CreateAgentBody
      // end-to-end. Include them ONLY when set — an empty title would write a
      // blank field and an empty reportsTo means "top level" (omit, don't send "").
      // Budget is deliberately NOT surfaced: per-agent budget is gated on a
      // separate cost_events bridge, not the owner's provider path.
      const body: {
        name: string;
        role: string;
        title?: string;
        reportsTo?: string;
      } = { name, role };
      const trimmedTitle = title.trim();
      if (trimmedTitle) body.title = trimmedTitle;
      if (reportsTo) body.reportsTo = reportsTo;
      await post("/agents", body);
      onCreated();
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  }

  if (mode === "choose") {
    return (
      <EditorialDialog
        open
        onOpenChange={(o) => !o && onClose()}
        title="New agent"
      >
        <AgentCreateChooser
          surface="teams"
          onHire={() => {
            onClose();
            openMarketplace({ view: "browse" });
          }}
          onBuild={() => setMode("build")}
        />
      </EditorialDialog>
    );
  }

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="New agent"
    >
      <div className="flex flex-col gap-3">
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          />
        </label>
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Senior backend engineer"
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          />
        </label>
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Role</span>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          >
            <option value="engineer">Engineer</option>
            <option value="ceo">CEO</option>
            <option value="manager">Manager</option>
          </select>
        </label>
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Reports to</span>
          <select
            aria-label="Reports to"
            value={reportsTo}
            onChange={(e) => setReportsTo(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          >
            <option value="">No manager (top level)</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        {err && (
          <div className="text-[var(--danger)] text-sm border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] rounded px-3 py-2">
            {err}
          </div>
        )}
        <div className="flex justify-end gap-2 mt-2">
          <EditorialButton variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </EditorialButton>
          <EditorialButton
            size="sm"
            onClick={submit}
            disabled={submitting || !name}
          >
            {submitting ? "Creating…" : "Create"}
          </EditorialButton>
        </div>
      </div>
    </EditorialDialog>
  );
}
