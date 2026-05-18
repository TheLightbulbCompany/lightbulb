"use client";

import { useState, useCallback } from "react";
import { Loader2, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";

import { capture } from "@/lib/analytics";
import { normalizeAgentId, validateAgentName } from "@/lib/agentIdentity";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";

interface AgentCreateFormProps {
  existingIds: string[];
  onCreated: () => void;
  onCancel: () => void;
}

export function AgentCreateForm({ existingIds, onCreated, onCancel }: AgentCreateFormProps) {
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const callRpc = useGatewayRpcMutation();

  const normalizedId = normalizeAgentId(name);
  const clientError = validateAgentName(name, existingIds);
  const canCreate = name.trim() !== "" && clientError === null && !creating;

  const handleCreate = useCallback(async () => {
    if (!canCreate) return;

    setCreating(true);
    setError(null);

    try {
      await callRpc("agents.create", {
        name: name.trim(),
        // OpenClaw's agents.create requires a non-empty `workspace` string —
        // it does NOT inherit agents.defaults.workspace at create time
        // (only at runtime via resolveAgentWorkspaceDir). Pass the same
        // path the default would resolve to, so the agent lands at
        // .openclaw/workspaces/{id}/ on EFS — matching main's explicit
        // workspaces/main override.
        workspace: ".openclaw/workspaces/" + normalizedId,
        reasoningDefault: "stream",
      });
      capture("agent_created", { agent_name: name.trim() });
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }, [canCreate, callRpc, name, onCreated]);

  return (
    <div className="rounded-lg border border-border bg-card/30 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium">New Agent</h3>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md p-1 text-muted-foreground hover:text-foreground hover:bg-[#f0ebe2] transition-colors"
        >
          <X className="h-4 w-4" />
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
            if (e.key === "Enter") handleCreate();
            if (e.key === "Escape") onCancel();
          }}
          autoFocus
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        />
        {normalizedId && !clientError && (
          <p className="text-[10px] text-muted-foreground">
            ID: {normalizedId}
          </p>
        )}
      </div>

      {/* Error display */}
      {(clientError || error) && (
        <p className="text-xs text-red-500">{clientError || error}</p>
      )}

      {/* Actions */}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={creating}>
          Cancel
        </Button>
        <Button size="sm" onClick={handleCreate} disabled={!canCreate}>
          {creating ? (
            <Loader2 className="h-3 w-3 animate-spin mr-1" />
          ) : (
            <Plus className="h-3 w-3 mr-1" />
          )}
          Create
        </Button>
      </div>
    </div>
  );
}
