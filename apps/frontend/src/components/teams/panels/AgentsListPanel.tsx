"use client";

import { useState } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Agent {
  id: string;
  name: string;
  role: string;
  status?: string;
}

export function AgentsListPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error, mutate } = read<{ agents: Agent[] }>("/agents");
  const [creating, setCreating] = useState(false);

  if (isLoading)
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Loading…
      </div>
    );
  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );

  const agents = data?.agents ?? [];

  return (
    <div className="editorial-page p-8 max-w-4xl">
      <div className="flex justify-between items-center mb-6">
        <h1 className="font-editorial-serif text-4xl text-[var(--ink)]">Agents</h1>
        <button
          onClick={() => setCreating(true)}
          className="editorial-btn editorial-btn--primary"
        >
          New agent
        </button>
      </div>

      <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
        {agents.map((a) => (
          <li
            key={a.id}
            className="flex justify-between items-center p-4"
          >
            <div>
              <div className="font-medium text-[var(--ink)]">{a.name}</div>
              <div className="text-xs text-[var(--mute)]">{a.role}</div>
            </div>
            <a
              href={`/teams/agents/${a.id}`}
              className="text-sm text-[var(--ink-soft)] hover:underline"
            >
              Open →
            </a>
          </li>
        ))}
        {agents.length === 0 && (
          <li className="p-4 font-editorial-serif text-xl italic text-[var(--mute)]">
            No agents yet.
          </li>
        )}
      </ul>

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
    <div className="fixed inset-0 bg-[rgba(26,26,26,0.35)] flex items-center justify-center">
      <div className="editorial-card p-6 w-96">
        <h2 className="font-editorial-serif text-2xl mb-4 text-[var(--ink)]">New agent</h2>
        <label className="block mb-3">
          <span className="text-sm text-[var(--ink-soft)]">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          />
        </label>
        <label className="block mb-4">
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
          <div className="text-[var(--danger)] text-sm mb-2 border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] rounded px-3 py-2">
            {err}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="editorial-btn editorial-btn--secondary editorial-btn--sm"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={submitting || !name}
            className="editorial-btn editorial-btn--primary editorial-btn--sm"
          >
            {submitting ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
