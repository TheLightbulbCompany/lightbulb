"use client";

import { useState } from "react";
import { Repeat } from "lucide-react";
import {
  EditorialButton,
  PanelDataTable,
  PanelEmptyState,
  PanelMasthead,
  PanelToolbar,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Routine {
  id: string;
  name: string;
  cron: string;
  agent_id: string;
  prompt?: string;
  enabled: boolean;
}

export function RoutinesPanel() {
  const { read, post, patch, del } = useTeamsApi();
  const { data, isLoading, error, mutate } = read<{ routines: Routine[] }>(
    "/routines",
  );
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const routines = data?.routines ?? [];
  const filtered = search
    ? routines.filter((r) =>
        r.name.toLowerCase().includes(search.toLowerCase()),
      )
    : routines;

  async function toggle(id: string, enabled: boolean) {
    setBusy(id);
    try {
      // PatchRoutineBody whitelists `enabled` (Task 3).
      await patch(`/routines/${id}`, { enabled });
      mutate();
    } finally {
      setBusy(null);
    }
  }

  async function remove(id: string) {
    if (!confirm("Delete this routine?")) return;
    setBusy(id);
    try {
      await del(`/routines/${id}`);
      mutate();
    } finally {
      setBusy(null);
    }
  }

  async function runNow(routine: Routine) {
    setBusy(routine.id);
    try {
      // Body forwarded to upstream `runRoutineSchema` — `reason: "manual"`
      // distinguishes UI-fired runs from cron/event-fired runs in upstream
      // run history.
      await post(`/routines/${routine.id}/run`, { reason: "manual" });
      mutate();
    } finally {
      setBusy(null);
    }
  }

  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );

  const columns: PanelDataColumn<Routine>[] = [
    {
      key: "name",
      header: "Name",
      cell: (r) => (
        <div>
          <div className="font-medium text-[var(--ink)]">{r.name}</div>
          <div className="text-xs text-[var(--mute)]">
            <span className="font-mono">{r.cron}</span> · agent {r.agent_id}
          </div>
        </div>
      ),
    },
    {
      key: "enabled",
      header: "Enabled",
      cell: (r) => (
        <label className="flex items-center gap-2 text-sm text-[var(--ink-soft)]">
          <input
            type="checkbox"
            checked={r.enabled}
            disabled={busy === r.id}
            onChange={(e) => toggle(r.id, e.target.checked)}
          />
          <span>{r.enabled ? "Enabled" : "Disabled"}</span>
        </label>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (r) => (
        <div className="flex items-center justify-end gap-2">
          <EditorialButton
            variant="ghost"
            size="sm"
            disabled={busy === r.id}
            aria-label={`Run ${r.name} now`}
            onClick={() => runNow(r)}
          >
            Run now
          </EditorialButton>
          <button
            onClick={() => remove(r.id)}
            disabled={busy === r.id}
            className="text-sm text-[var(--mute)] hover:text-[var(--danger)] hover:underline"
          >
            Delete
          </button>
        </div>
      ),
    },
  ];

  return (
    <div className="editorial-page flex flex-col gap-6 p-4 sm:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Routines"
        title="Routines"
        actions={
          <EditorialButton
            variant="primary"
            onClick={() => setCreating(true)}
          >
            New routine
          </EditorialButton>
        }
      />
      <PanelToolbar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Find routine..."
        searchAriaLabel="Search routines"
      />
      {isLoading ? (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Loading…
        </div>
      ) : filtered.length === 0 ? (
        <PanelEmptyState
          icon={Repeat}
          headline="No routines yet."
          supporting={
            search
              ? "No routines match your search."
              : "Create a routine to define a recurring workflow that fires on a schedule or trigger."
          }
        />
      ) : (
        <PanelDataTable<Routine>
          rows={filtered}
          columns={columns}
          rowKey={(r) => r.id}
        />
      )}

      {creating && (
        <CreateRoutineDialog
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

function CreateRoutineDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  const [name, setName] = useState("");
  const [cron, setCron] = useState("0 9 * * *");
  const [agentId, setAgentId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setSubmitting(true);
    setErr(null);
    try {
      // CreateRoutineBody fields (Task 3): name, cron, agent_id, prompt, enabled.
      await post("/routines", {
        name,
        cron,
        agent_id: agentId,
        prompt,
        enabled,
      });
      onCreated();
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-[rgba(26,26,26,0.35)] flex items-center justify-center">
      <div className="editorial-card p-6 w-[28rem]">
        <h2 className="font-editorial-serif text-2xl mb-4 text-[var(--ink)]">New routine</h2>
        <label className="block mb-3">
          <span className="text-sm text-[var(--ink-soft)]">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          />
        </label>
        <label className="block mb-3">
          <span className="text-sm text-[var(--ink-soft)]">Cron</span>
          <input
            value={cron}
            onChange={(e) => setCron(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 font-mono text-[var(--ink)]"
          />
        </label>
        <label className="block mb-3">
          <span className="text-sm text-[var(--ink-soft)]">Agent ID</span>
          <input
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          />
        </label>
        <label className="block mb-3">
          <span className="text-sm text-[var(--ink-soft)]">Prompt</span>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
            rows={3}
          />
        </label>
        <label className="flex items-center gap-2 mb-4 text-sm text-[var(--ink-soft)]">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          <span>Enabled</span>
        </label>
        {err && (
          <div className="text-[var(--danger)] text-sm mb-2 border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] rounded px-3 py-2">
            {err}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <EditorialButton
            variant="secondary"
            size="sm"
            onClick={onClose}
          >
            Cancel
          </EditorialButton>
          <EditorialButton
            variant="primary"
            size="sm"
            onClick={submit}
            disabled={submitting || !name || !cron || !agentId || !prompt}
          >
            {submitting ? "Creating…" : "Create"}
          </EditorialButton>
        </div>
      </div>
    </div>
  );
}
