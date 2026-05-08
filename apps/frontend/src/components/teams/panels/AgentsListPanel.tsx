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
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Agent {
  id: string;
  name: string;
  role: string;
  status?: string;
}

export function AgentsListPanel() {
  const { read } = useTeamsApi();
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
      key: "open",
      header: "",
      align: "right",
      cell: (a) => (
        <a
          href={`/teams/agents/${a.id}`}
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
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  const [name, setName] = useState("");
  const [role, setRole] = useState("engineer");
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
      await post("/agents", { name, role });
      onCreated();
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
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
