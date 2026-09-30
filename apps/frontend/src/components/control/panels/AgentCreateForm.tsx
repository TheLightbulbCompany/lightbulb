"use client";

import { useState, useCallback } from "react";
import { Loader2, X } from "lucide-react";
import { Button } from "@/components/ui/button";

import { capture } from "@/lib/analytics";
import { validateAgentName } from "@/lib/agentIdentity";
import { useRuntimeRpcMutation } from "@/hooks/useRuntimeRpc";
import { AgentPaperclipFields } from "@/components/control/agents/AgentPaperclipFields";
import type { PaperclipMeta } from "@/lib/paperclipMeta";

interface AgentCreateFormProps {
  existingNames: string[];
  onCreated: () => void;
  onCancel: () => void;
}

export function AgentCreateForm({ existingNames, onCreated, onCancel }: AgentCreateFormProps) {
  const [name, setName] = useState("");
  const [meta, setMeta] = useState<PaperclipMeta>({ role: "general" });
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const callRpc = useRuntimeRpcMutation();

  const clientError = validateAgentName(name, existingNames);
  const canCreate = name.trim() !== "" && clientError === null && !creating;

  const handleCreate = useCallback(async () => {
    if (!canCreate) return;
    setCreating(true);
    setError(null);
    try {
      // Build params.paperclip payload — only include keys with non-empty values
      // so the backend's paperclip_meta.read_from_agent_entry doesn't have to
      // sanitize whitespace-only values into None.
      const paperclipBlock: Record<string, string> = { role: meta.role };
      if (meta.title && meta.title.trim()) paperclipBlock.title = meta.title.trim();
      if (meta.capabilities && meta.capabilities.trim())
        paperclipBlock.capabilities = meta.capabilities.trim();
      if (meta.icon && meta.icon.trim()) paperclipBlock.icon = meta.icon.trim();

      await callRpc(
        "agents.create",
        {
          name: name.trim(),
          // No `workspace`: the backend fills the layout for the owner's runtime (an EFS path on
          // ECS, nothing on Hermes, where a profile dir IS the state root). A client that names a
          // path bakes one runtime's filesystem into every client release.
          reasoningDefault: "stream",
          params: { paperclip: paperclipBlock },
        },
      );
      capture("agent_created", { paperclip_role: meta.role });
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }, [canCreate, callRpc, name, meta, onCreated]);

  return (
    <div className="rounded-lg border border-border bg-cream-deep p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium">New Agent</h3>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md p-1 text-muted-foreground hover:text-foreground hover:bg-cream-deep transition-colors"
        >
          <X size={16} />
        </button>
      </div>

      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Name</label>
        <input
          type="text"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setError(null);
          }}
          placeholder="e.g. Research Assistant"
          disabled={creating}
          onKeyDown={(e) => {
            if (e.key === "Escape") onCancel();
          }}
          autoFocus
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm disabled:opacity-50"
        />
      </div>

      <AgentPaperclipFields value={meta} onChange={setMeta} disabled={creating} />

      {(clientError || error) && (
        <p className="text-xs text-err">{clientError || error}</p>
      )}

      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={creating}>
          Cancel
        </Button>
        <Button size="sm" onClick={handleCreate} disabled={!canCreate}>
          {creating ? <Loader2 size={12} strokeWidth={1.75} className="animate-spin" /> : "Create"}
        </Button>
      </div>
    </div>
  );
}
